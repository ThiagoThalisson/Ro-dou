"""
Dynamic DAG generator integrated with YAML config system to create DAG
which searchs terms in the Gazzete [Diário Oficial da União-DOU] and
send it by email to the  provided `recipient_emails` list. The DAGs are
generated by YAML config files at `dag_confs` folder.

TODO:
[] - setar CONFIG_FILEPATH dinamicamente
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

def parse_regex(raw_html):
    clean_re = re.compile(r'(.*?)<.*?>(.*?)<.*?>')
    groups = clean_re.match(raw_html).groups()
    return groups[0], groups[1]

def is_signature(result, search_term):
    """Verifica se o `search_term` (geralmente usado para busca por nome
    de pessoas) está presente na assinatura. Para isso se utiliza de um
    "bug" da API que, para estes casos, retorna o `abstract` iniciando
    com a assinatura do documento, o que não ocorre quando o match
    acontece em outras partes do documento. Dessa forma esta função
    checa se isso ocorreu (str.startswith()) e é utilizada para filtrar
    os resultados presentes no relatório final. Também resolve os casos
    em que o nome da pessoa é parte de nome maior. Por exemplo o nome
    'ANTONIO DE OLIVEIRA' é parte do nome 'JOSÉ ANTONIO DE OLIVEIRA MATOS'
    """
    norm_term = unidecode(search_term).lower()
    abstract = result.get('abstract')
    clean_abstract = clean_html(abstract)
    start_name, match_name = parse_regex(abstract)

    norm_abstract = unidecode(clean_abstract).lower()
    norm_abstract_withou_start_name = norm_abstract[len(start_name):]

    return (
        # As assinaturas são sempre uppercase
        (start_name + match_name).isupper() and
            # Resolve os casos 'Antonio de Oliveira' e 'Antonio de Oliveira Matos'
            (norm_abstract.startswith(norm_term) or
            # Resolve os casos 'José Antonio de Oliveira' e ' José Antonio de Oliveira Matos'
             norm_abstract_withou_start_name.startswith(norm_term))
    )

def search_all_terms(term_list,
                     dou_sections,
                     search_date,
                     field,
                     is_exact_search,
                     ignore_signature_match,
                     force_rematch):
    search_results = {}
    dou_hook = DOUHook()
    for search_term in term_list:
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

    return search_results

def _exec_dou_search(term_list,
                     dou_sections: [str],
                     search_date,
                     field,
                     is_exact_search: bool,
                     ignore_signature_match: bool,
                     force_rematch: bool):
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

    search_results = search_all_terms(term_list,
                                      dou_sections,
                                      search_date,
                                      field,
                                      is_exact_search,
                                      ignore_signature_match,
                                      force_rematch)

    if term_group_map:
        groups = sorted(list(set(term_group_map.values())))
        grouped_result = {
            g1:{
                t: search_results[t]
                for (t, g2) in sorted(term_group_map.items())
                if t in search_results and g1 == g2}
            for g1 in groups}
    else:
        grouped_result = {'single_group': search_results}

    # Clear empty groups
    trimmed_result = {k: v for k, v in grouped_result.items() if v}

    return trimmed_result

def _send_email_task(search_report, subject, email_to_list,
                     attach_csv, dag_id):
    """
    Envia e-mail de notificação dos aspectos mais relevantes do DOU.
    """
    search_report = ast.literal_eval(search_report)

    # Don't send empty email
    if not search_report:
        return

    today_date = date.today().strftime("%d/%m/%Y")
    full_subject = f"{subject} - DOU de {today_date}"
    content = """
        <style>
            .grupo {
                border-top: 2px solid #707070;
                padding: 20px 0;
            }
            .resultado {
                border-bottom: 1px solid #707070;
                padding: 20px 20px;
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
    for group, results in search_report.items():
        if results:
            if group is 'single_group':
                content += '<div style="margin: 0 -20px;">'
            else:
                content += f"""<div class='grupo'>
                    <p class='search-total-label'>
                    Grupo: <b>{group}</b></p>
                """
            for term, items in results.items():
                if items:
                    content += f"""<div class='resultado'>
                            <p class='search-total-label'>
                            Resultados para: <b>{term}</b></p>"""

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
                        new_table.append((
                            group,
                            term,
                            sec_desc,
                            item['href'],
                            item['title'],
                            item['abstract'],
                            item['date'],
                            ))
                    content += "</div>"
            content += "</div>"

    if attach_csv:
        df = pd.DataFrame(new_table)
        df.columns = ['Grupo', 'Termo de pesquisa', 'Seção', 'URL',
                      'Título', 'Resumo', 'Data']
        if 'single_group' in search_report:
            del df['Grupo']

        tmp_dir = os.path.join(
            Variable.get("path_tmp"),
            LOCAL_TMP_DIR,
            dag_id)
        os.makedirs(tmp_dir, exist_ok=True)
        file = os.path.join(tmp_dir, 'extracao_dou.csv')
        df.to_csv(file, index=False)
        files = [file]
    else:
        files = None

    send_email(to=email_to_list,
               subject=full_subject,
               files=files,
               html_content=content,
               mime_charset='utf-8')

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
               force_rematch,
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
                "force_rematch": force_rematch,
                },
        )
        if sql:
            select_terms_from_db >> exec_dou_search

        send_email_task = PythonOperator(
            task_id='send_email_task',
            python_callable=_send_email_task,
            op_kwargs={
                "search_report": "{{ ti.xcom_pull(task_ids='exec_dou_search') }}",
                "subject": subject,
                "email_to_list": email_to_list,
                "attach_csv": attach_csv,
                "dag_id": dag_id,
                },
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
    force_rematch = search.get('force_rematch', False)
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
        force_rematch,
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
