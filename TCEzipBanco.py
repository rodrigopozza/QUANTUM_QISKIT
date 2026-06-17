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
        "cd
