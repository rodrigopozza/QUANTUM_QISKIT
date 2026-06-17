import os
import base64
import json
import time
import requests
import zipfile
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import psycopg2
from psycopg2 import sql

# 1. Configurações fixas para Paranavaí em 2026
CODIGO_MUNICIPIO = "18402"
NOME_MUNICIPIO = "PARANAVAÍ"
ANO = "2026"

# Pasta final onde os XMLs extraídos serão salvos
PASTA_DESTINO = f"./downloads_tce_pr/{NOME_MUNICIPIO}/{ANO}"
os.makedirs(PASTA_DESTINO, exist_ok=True)

# 2. Configurações do Banco de Dados
DB_CONFIG = {
    "dbname": "postgres",
    "user": "postgres",
    "password": "supozza7",
    "host": "localhost",  # Altere se o banco estiver em outra máquina
    "port": "5432"
}
SCHEMA_NAME = "Paranavai"

def obter_conexao():
    """Retorna uma conexão ativa com o PostgreSQL"""
    return psycopg2.connect(**DB_CONFIG)

def inicializar_schema():
    """Garante que o esquema Paranavai exista no banco"""
    conn = obter_conexao()
    cursor = conn.cursor()
    try:
        cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA_NAME)))
        conn.commit()
        print(f"[DB] Esquema '{SCHEMA_NAME}' verificado/criado com sucesso.")
    except Exception as e:
        print(f"[DB] Erro ao criar esquema: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

def normalizar_nome(nome):
    """Normaliza nomes de tags/arquivos para evitar erros de sintaxe no SQL"""
    return nome.lower().replace("-", "_").replace(".", "_")

def importar_xml_para_banco(caminho_xml):
    """Lê um arquivo XML de forma dinâmica (atributos ou tags internas) e o insere no PostgreSQL"""
    if not os.path.exists(caminho_xml):
        return

    print(f"  [DB] Processando inserção do arquivo: {os.path.basename(caminho_xml)}...")
    
    try:
        tree = ET.parse(caminho_xml)
        root = tree.getroot()
    except Exception as e:
        print(f"  [-] Erro ao ler/parsear o XML {caminho_xml}: {e}")
        return

    # Nome da tabela será baseado no nome do arquivo XML (ex: dados_1.xml -> dados_1)
    nome_tabela = normalizar_nome(os.path.splitext(os.path.basename(caminho_xml))[0])
    
    colunas_set = set()
    registros = []
    
    # Varre os elementos filhos do nó raiz (ex: cada tag <Combustivel />)
    for elemento in root:
        registro_dados = {}
        
        # --- ADAPTAÇÃO DINÂMICA ---
        # 1. Se o registro possuir atributos no cabeçalho da tag (Formato do arquivo Combustivel)
        if elemento.attrib:
            for attr_nome, attr_valor in elemento.attrib.items():
                col_nome = normalizar_nome(attr_nome)
                colunas_set.add(col_nome)
                registro_dados[col_nome] = attr_valor if attr_valor else ""
        
        # 2. Se o registro possuir sub-tags internas (Formato XML Tradicional)
        for filho in elemento:
            col_nome = normalizar_nome(filho.tag)
            colunas_set.add(col_nome)
            registro_dados[col_nome] = filho.text if filho.text else ""
            
        if registro_dados:
            registros.append(registro_dados)

    if not colunas_set or not registros:
        print(f"  [-] O XML {nome_tabela} não possui registros estruturados detectáveis.")
        return

    colunas = sorted(list(colunas_set))
    
    conn = obter_conexao()
    cursor = conn.cursor()
    
    try:
        # Cria a tabela dinamicamente com todas as colunas como TEXT para evitar quebras por tipagem
        colunas_sql = [sql.SQL("{} TEXT").format(sql.Identifier(col)) for col in colunas]
        
        query_criar_tabela = sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
            sql.Identifier(SCHEMA_NAME),
            sql.Identifier(nome_tabela),
            sql.SQL(', ').join(colunas_sql)
        )
        cursor.execute(query_criar_tabela)
        
        # Prepara a query para inserção em lote (Batch Insert)
        query_insercao = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
            sql.Identifier(SCHEMA_NAME),
            sql.Identifier(nome_tabela),
            sql.SQL(', ').join(map(sql.Identifier, colunas)),
            sql.SQL(', ').join([sql.Placeholder()] * len(colunas))
        )
        
        for reg in registros:
            # Garante None (NULL no banco) para colunas que porventura faltem em algum registro específico
            valores = [reg.get(col, None) for col in colunas]
            cursor.execute(query_insercao, valores)
            
        conn.commit()
        print(f"  [V] Sucesso: {len(registros)} registros inseridos na tabela '{SCHEMA_NAME}.{nome_tabela}'.")
        
    except Exception as e:
        print(f"  [-] Erro ao inserir dados no banco para a tabela {nome_tabela}: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

def gerar_url_consulta():
    """Gera a URL em Base64 exigida pelo TCE-PR"""
    dados_busca = {
        "cdMunicipio": CODIGO_MUNICIPIO,
        "nrAno": ANO,
        "municipio": NOME_MUNICIPIO
    }
    json_string = json.dumps(dados_busca, ensure_ascii=False)
    base64_string = base64.b64encode(json_string.encode('utf-8')).decode('utf-8')
    return f"https://pit.tce.pr.gov.br/Dados/DadosConsulta/Consulta/?f={base64_string}"

def baixar_e_extrair_zip():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })

    print(f"[+] Acessando o painel do TCE-PR para: {NOME_MUNICIPIO} ({ANO})...")
    url_consulta = gerar_url_consulta()

    try:
        resposta = session.get(url_consulta, timeout=15)
        if resposta.status_code != 200:
            print(f"[-] Erro ao acessar a página. Status: {resposta.status_code}")
            return

        soup = BeautifulSoup(resposta.text, 'html.parser')

        links_arquivos = []
        for tag_a in soup.find_all('a', href=True):
            href = tag_a['href']
            if any(termo in href.lower() for termo in ['.xml', '.zip', 'download', 'obterarquivo']):
                if href.startswith('/'):
                    href = f"https://pit.tce.pr.gov.br{href}"
                links_arquivos.append(href)

        if not links_arquivos:
            print(f"[-] Nenhum arquivo encontrado para Paranavaí em {ANO}.")
            return

        print(f"[V] Encontrados {len(links_arquivos)} pacotes de arquivos para baixar.")
        
        # Garante a existência do Esquema no Banco antes das inserções
        inicializar_schema()

        for indice, url_download in enumerate(links_arquivos):
            print(f"\n---> Processando pacote {indice + 1}/{len(links_arquivos)}...")

            arquivo_res = session.get(url_download, timeout=45)
            if arquivo_res.status_code == 200:

                caminho_zip = os.path.join(PASTA_DESTINO, f"pacote_{indice + 1}.zip")

                with open(caminho_zip, 'wb') as f:
                    f.write(arquivo_res.content)

                arquivos_para_banco = []

                try:
                    if zipfile.is_zipfile(caminho_zip):
                        with zipfile.ZipFile(caminho_zip, 'r') as zip_ref:
                            nomes_arquivos = zip_ref.namelist()
                            zip_ref.extractall(PASTA_DESTINO)
                            
                            for nome in nomes_arquivos:
                                if nome.lower().endswith('.xml'):
                                    arquivos_para_banco.append(os.path.join(PASTA_DESTINO, nome))
                                    
                        print(f"   [V] Pacote {indice + 1} extraído com sucesso.")
                    else:
                        # Se o arquivo foi baixado diretamente como um XML isolado
                        caminho_xml = os.path.join(PASTA_DESTINO, f"dados_{indice + 1}.xml")
                        os.rename(caminho_zip, caminho_xml)
                        arquivos_para_banco.append(caminho_xml)
                        print(f"   [V] Arquivo baixado diretamente como XML.")

                    # Faz a leitura híbrida e insere os dados salvos no banco
                    for xml_file in arquivos_para_banco:
                        importar_xml_para_banco(xml_file)

                except Exception as erro_zip:
                    print(f"   [-] Erro ao processar o ZIP {indice + 1}: {erro_zip}")
                finally:
                    if os.path.exists(caminho_zip) and zipfile.is_zipfile(caminho_zip):
                        os.remove(caminho_zip)
            else:
                print(f"   [-] Falha ao baixar o pacote: {url_download}")

            time.sleep(1)

    except Exception as e:
        print(f"[-] Ocorreu um erro durante a automação: {e}")

if __name__ == "__main__":
    baixar_e_extrair_zip()
    print("\n[FIM] Todos os arquivos foram processados e salvos no banco de dados!")
