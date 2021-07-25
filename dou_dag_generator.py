"""
Dynamic DAG generator integrated with YAML config system to create DAG
which searchs terms in the Gazzete [Diário Oficial da União-DOU] and
send it by email to the  provided `recipient_emails` list. The DAGs are
generated by YAML config files at `dag_confs` folder.

TODO:
[] - Escrever tutorial no portal cginf
"""

from datetime import date, datetime, timedelta
import os
import ast
import time
import re
from random import random
import pandas as pd
import yaml
from unidecode import unidecode

from airflow import DAG
from airflow.models import Variable
from airflow.operators.python_operator import PythonOperator
from airflow.hooks.mssql_hook import MsSqlHook
from airflow.utils.email import send_email

from FastETL.custom_functions.utils.encode_html import replace_to_html_encode
from FastETL.hooks.dou_hook import DOUHook, Section, SearchDate, Field
from airflow_commons.slack_messages import send_slack

CONFIG_FILEPATH = '/usr/local/airflow/dags/dou/dag_confs/'
LOCAL_TMP_DIR = 'dou-dag-generator'
DEFAULT_SCHEDULE = '0 2 * * *'
SCRAPPING_INTERVAL = 1


def clean_html(raw_html):
    clean_re = re.compile('<.*?>')
    clean_text = re.sub(clean_re, '', raw_html)
    return clean_text

def is_signature(result, search_term):
    clean_abstract = clean_html(result.get('abstract'))
    norm_abstract = unidecode(clean_abstract).lower()
    norm_term = unidecode(search_term).lower()

    return norm_abstract.startswith(norm_term)

def _exec_dou_search(term_list,
                     dou_sections: [str],
                     search_date,
                     field,
                     is_exact_search: bool,
                     ignore_signature_match: bool):
    dou_hook = DOUHook()

    term_group_map = None
    if isinstance(term_list, str):
        # Quando `term_list` vem do xcom (tipo str) é necessário recriar
        # o dataframe a partir da string
        terms_df = pd.read_json(term_list)
        first_column = terms_df.iloc[:, 0]
        term_list = first_column.tolist()

        # Se existir a segunda coluna usada para agrupar é pq existem grupos
        if len(terms_df.columns) > 1:
            second_column = terms_df.iloc[:, 1]
            term_group_map = dict(zip(first_column, second_column))

    search_results = {}
    # TODO REMOVER O LIMITE DE 10 DA LISTA DO FOR
    for search_term in term_list[:1]:
    # for search_term in term_list:
        results = dou_hook.search_text(search_term,
                                       [Section[s] for s in dou_sections],
                                       SearchDate[search_date],
                                       Field[field],
                                       is_exact_search
                                       )
        if ignore_signature_match:
            results = [r for r in results if not is_signature(r, search_term)]
        if results:
            search_results[search_term] = results

        time.sleep(SCRAPPING_INTERVAL * random() * 2)

    return search_results, term_group_map

def _send_email_task(results, subject, email_to_list, attach_csv, dag_id):
    """
    Envia e-mail de notificação dos aspectos mais relevantes do DOU.
    """
    results = ast.literal_eval(results)
    if not results:
        return

    today_date = date.today().strftime("%d/%m/%Y")
    full_subject = f"{subject} - DOU de {today_date}"


    content = """
        <style>
            .resultado {
                border-bottom: 1px solid #707070;
                padding: 20px 0;
            }
            .search-total-label {
                font-size: 15px; margin: 0;
                padding: 0;
            }
            .secao-marker {
                color: #06acff;
                font-family: 'rawline',sans-serif;
                font-size: 18px;
                font-weight: bold;
                margin-bottom: 8px;
            }
            .title-marker {
                font-family: 'rawline',sans-serif;
                font-size: 20px;
                font-weight: bold;
                line-height: 26px;
                margin-bottom: 8px;
                margin-top: 0;
            }
            .title-marker a {
                color: #222;
                margin: 0;
                text-decoration: none;
                text-transform: uppercase;
            }
            .title-marker a:hover {
                text-decoration: underline;
            }
            .abstract-marker {
                font-size: 18px;
                font-weight: 500;
                line-height: 22px;
                max-height: 44px;
                margin-bottom: 5px;
                margin-top: 0;
                overflow: hidden;
            }
            .date-marker {
                color: #b1b1b1;
                font-family: 'rawline', sans-serif;
                font-size: 14px;
                font-weight: 500;
                margin-top: 0;
            }
        </style>
    """

    new_table = []
    for term, items in results.items():
        content += f"""<div class='resultado'>
            <p class='search-total-label'>{len(items)} resultado"""
        content += (' ' if len(items) == 1 else 's ')
        content += f"""para <b>{term}</b></p>"""

        if len(items) > 0:
            for item in items:
                sec_desc = DOUHook.SEC_DESCRIPTION[item['section']]
                content += f"""<br>
                    <p class="secao-marker">{sec_desc}</p>
                    <h5 class='title-marker'>
                    <a href='{item['href']}'>{item['title']}</a>
                    </h5>
                    <p class='abstract-marker'>{item['abstract']}</p>
                    <p class='date-marker'>{item['date']}</p>
                """
                new_table.append((term,
                                  sec_desc,
                                  item['href'],
                                  item['title'],
                                  item['abstract'],
                                  item['date'],
                                  ))
        content += "</div>"

    files = None
    if attach_csv:
        df = pd.DataFrame(new_table)
        df.columns = ['Termo de pesquisa', 'Seção', 'URL',
                      'Título', 'Resumo', 'Data']
        tmp_dir = os.path.join(
            Variable.get("path_tmp"),
            LOCAL_TMP_DIR,
            dag_id
        )
        os.makedirs(tmp_dir, exist_ok=True)
        file = os.path.join(tmp_dir, 'extracao_dou.csv')
        df.to_csv(file, index=False)
        files = [file]

    send_email(to=email_to_list,
               subject=full_subject,
               files=files,
               html_content=replace_to_html_encode(content))

def _select_terms_from_db(sql, conn_id):
    """Executa o `sql` e retorna a lista de termos que serão utilizados
    posteriormente na pesquisa no DOU. A primeira coluna do select deve
    conter os termos a serem pesquisados. A segunda coluna, que é
    opicional, é um classificador que será utilizado para agrupar e
    ordenar o relatório por email e o CSV gerado.
    """
    mssql_hook = MsSqlHook(mssql_conn_id=conn_id)
    df = mssql_hook.get_pandas_df(sql)
    # Remove espaços desnecessários e troca null por ''
    df= df.applymap(lambda x: str.strip(x) if pd.notnull(x) else '')

    return df.to_json(orient="columns")


def create_dag(dag_id,
               dou_sections,
               search_date,
               search_field,
               is_exact_search,
               ignore_signature_match,
               term_list,
               sql,
               conn_id,
               email_to_list,
               subject,
               attach_csv,
               schedule,
               description,
               tags):
    """Cria a DAG, suas tasks, a orquestração das tasks e retorna a DAG."""
    default_args = {
        'owner': 'yaml-dag-generator',
        'start_date': datetime(2021, 6, 18),
        'depends_on_past': False,
        'retries': 5,
        'retry_delay': timedelta(minutes=20),
        'on_retry_callback': send_slack,
        'on_failure_callback': send_slack,
    }
    dag = DAG(
        dag_id,
        default_args=default_args,
        schedule_interval=schedule,
        description=description,
        catchup=False,
        tags=tags
        )

    with dag:
        queue = None
        if sql:
            select_terms_from_db = PythonOperator(
                task_id='select_terms_from_db',
                python_callable=_select_terms_from_db,
                op_kwargs={
                    "sql": sql,
                    "conn_id": conn_id,
                    }
            )
            term_list = "{{ ti.xcom_pull(task_ids='select_terms_from_db') }}"
            queue = 'local'

        exec_dou_search = PythonOperator(
            task_id='exec_dou_search',
            python_callable=_exec_dou_search,
            op_kwargs={
                "term_list": term_list,
                "dou_sections": dou_sections,
                "search_date": search_date,
                "field": search_field,
                "is_exact_search": is_exact_search,
                "ignore_signature_match": ignore_signature_match,
                },
            queue=queue,
        )
        if sql:
            select_terms_from_db >> exec_dou_search

        send_email_task = PythonOperator(
            task_id='send_email_task',
            python_callable=_send_email_task,
            op_kwargs={
                "results": "{{ ti.xcom_pull(task_ids='exec_dou_search') }}",
                "subject": subject,
                "email_to_list": email_to_list,
                "attach_csv": attach_csv,
                "dag_id": dag_id,
                },
            queue=queue,
        )
        exec_dou_search >> send_email_task

    return dag


def parse_yaml_file(file_name):
    """Process the config file in order to instantiate the DAG in Airflow."""
    def try_get(variable: dict, field, error_msg=None):
        """Try to retrieve the property named as `field` from
        `variable` dict and raise apropriate message"""
        try:
            return variable[field]
        except KeyError:
            if not error_msg:
                error_msg = f'O campo `{field}` é obrigatório.'
            error_msg = f'Erro no arquivo {file_name}: {error_msg}'
            raise ValueError(error_msg)

    def get_terms_params(search):
        terms = try_get(search, 'terms')
        sql = None
        conn_id = None
        if isinstance(terms, dict):
            if 'from_airflow_variable' in terms:
                var_name = terms.get('from_airflow_variable')
                terms = ast.literal_eval(Variable.get(var_name))
            elif 'from_db_select' in terms:
                from_db_select = terms.get('from_db_select')
                sql = try_get(from_db_select, 'sql')
                conn_id = try_get(from_db_select, 'conn_id')
            else:
                raise ValueError('O campo `terms` aceita como valores válidos '
                                 'uma lista de strings ou parâmetros do tipo '
                                 '`from_airflow_variable` ou `from_db_select`.')
        return terms, sql, conn_id

    def hashval(string, size):
        _hash = 0
        # Take ordinal number of char in string, and just add
        for x in string:
            _hash += (ord(x))
        return _hash % size # Depending on the range, do a modulo operation.

    def get_safe_schedule(dag: DAG):
        """Retorna um novo valor de `schedule_interval` randomizando o
        minuto de execução baseado no `dag_id`, caso a dag utilize o
        schedule_interval padrão. Aplica uma função de hash na string
        dag_id que retorna valor entre 0 e 60 que define o minuto de
        execução.
        """
        schedule = dag.get('schedule_interval', DEFAULT_SCHEDULE)
        if schedule == DEFAULT_SCHEDULE:
            dag_id = try_get(dag, 'id')
            schedule_without_min = ' '.join(schedule.split(" ")[1:])
            id_based_minute = hashval(dag_id, 60)
            schedule = f'{id_based_minute} {schedule_without_min}'
        return schedule

    with open(CONFIG_FILEPATH + file_name, 'r') as file:
        dag_config_dict = yaml.safe_load(file)
    dag = try_get(dag_config_dict, 'dag')
    dag_id = try_get(dag, 'id')
    description = try_get(dag, 'description')
    report = try_get(dag, 'report')
    emails = try_get(report, 'emails')
    search = try_get(dag, 'search')
    terms, sql, conn_id = get_terms_params(search)

    # Optional fields
    dou_sections = search.get('dou_sections', ['TODOS'])
    search_date = search.get('date', 'DIA')
    field = search.get('field', 'TUDO')
    is_exact_search = search.get('is_exact_search', True)
    ignore_signature_match = search.get('ignore_signature_match', False)
    schedule = get_safe_schedule(dag)
    dag_tags = dag.get('tags', [])
    # add default tags
    dag_tags.append('dou')
    dag_tags.append('generated_dag')
    subject = report.get('subject', 'Extraçao do DOU')
    attach_csv = report.get('attach_csv', False)
    globals()[dag_id] = create_dag(
        dag_id,
        dou_sections,
        search_date,
        field,
        is_exact_search,
        ignore_signature_match,
        terms,
        sql,
        conn_id,
        emails,
        subject,
        attach_csv,
        schedule,
        description,
        dag_tags,
        )

yaml_files = [
    f for f in os.listdir(CONFIG_FILEPATH)
    if f.split('.')[-1] in ['yaml', 'yml']
]
for filename in yaml_files:
    parse_yaml_file(filename)
