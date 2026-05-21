# SBACloudCompare

Aplicação web para inventário de recursos em cloud e comparação de serviços entre provedores, com foco inicial em AWS e Azure.

## Visão geral

- Leitura de recursos AWS com exportação em Excel multiaba.
- Leitura de recursos Azure com exportação em Excel multiaba.
- Equivalência entre provedores usando a base do CompareCloud.
- Interface web com explicação dos campos e download do arquivo final.
- Estrutura pronta para deploy em Cloud Run.

## Como a autenticação funciona

- AWS: você informa `Access Key ID`, `Secret Access Key` e, opcionalmente, `Session Token` no formulário.
- Azure: você informa `Tenant ID`, `Client ID` e `Client Secret` da service principal.
- As credenciais são usadas apenas durante a execução da leitura.
- As credenciais não são gravadas em `.env`, não vão para arquivo local e não ficam hardcoded no código.
- O arquivo é gerado e retornado na mesma requisição (`scan -> download` direto), sem armazenamento para reutilização no servidor.

## Fonte de equivalência (CompareCloud)

- Site: `https://comparecloud.in/`
- Dataset remoto: `https://raw.githubusercontent.com/ilyas-it83/CloudComparer/main/_data/cloudservices.yml`
- Fallback local: `app/data/cloudservices_snapshot.yml`

Se o dataset remoto estiver indisponível, a aplicação usa o snapshot local automaticamente.

## Requisitos locais

1. Python 3.12 ou superior.
2. `pip` atualizado.
3. Opcional: `venv` para ambiente virtual.

## Rodando localmente (passo a passo)

1. Entre na pasta do projeto.
```bash
cd SBACloudCompare
```

2. Crie e ative um ambiente virtual.
```bash
python -m venv .venv
```
```bash
# Linux/macOS
source .venv/bin/activate
```
```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

3. Instale dependências.
```bash
pip install -r requirements.txt
```

4. Rode a aplicação.
```bash
uvicorn app.main:app --reload --port 8080
```

5. Teste endpoints básicos.
```bash
curl http://localhost:8080/healthz
```

6. Abra no navegador.
```text
http://localhost:8080
```

## Deploy no Cloud Run (guia completo)

### 1) Pré-requisitos GCP

1. Projeto GCP criado e com billing ativo.
2. `gcloud` instalado e atualizado.
3. Permissões IAM para deploy. Exemplo mínimo:
- `roles/run.admin`
- `roles/iam.serviceAccountUser`
- `roles/cloudbuild.builds.editor`
- `roles/artifactregistry.admin` (ou papéis equivalentes para usar repositório existente)

### 2) Login e configuração inicial

1. Login no Google Cloud.
```bash
gcloud auth login
```

2. Defina variáveis de ambiente (Linux/macOS).
```bash
export PROJECT_ID="seu-projeto-gcp"
export REGION="us-central1"
export SERVICE_NAME="sba-cloud-compare"
export REPO_NAME="cloud-run-images"
```

3. Defina variáveis de ambiente (Windows PowerShell).
```powershell
$env:PROJECT_ID="seu-projeto-gcp"
$env:REGION="us-central1"
$env:SERVICE_NAME="sba-cloud-compare"
$env:REPO_NAME="cloud-run-images"
```

4. Selecione o projeto no `gcloud`.
```bash
gcloud config set project $PROJECT_ID
```

### 3) Habilite APIs necessárias

```bash
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable artifactregistry.googleapis.com
```

### 4) Crie repositório no Artifact Registry (uma vez)

```bash
gcloud artifacts repositories create $REPO_NAME \
  --repository-format=docker \
  --location=$REGION \
  --description="Imagens Docker para SBACloudCompare"
```

### 5) Build da imagem

```bash
gcloud builds submit \
  --tag $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$SERVICE_NAME:latest
```

### 6) Deploy no Cloud Run

```bash
gcloud run deploy $SERVICE_NAME \
  --image $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$SERVICE_NAME:latest \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --timeout 3600 \
  --cpu 2 \
  --memory 2Gi \
  --concurrency 1 \
  --max-instances 1 \
  --min-instances 0
```

### 7) Validar deploy

1. Obtenha URL do serviço.
```bash
gcloud run services describe $SERVICE_NAME --region $REGION --format='value(status.url)'
```

2. Teste o healthcheck.
```bash
curl https://SUA_URL/healthz
```

3. Abra a URL no navegador e execute uma leitura AWS ou Azure.

## Sobre `max-instances=1` e `concurrency=1` neste momento

- A recomendação acima é conservadora para controlar custo e consumo de API em scans longos.
- Como o download é imediato na mesma requisição, não há dependência de cache de arquivo entre chamadas.
- Se você precisar de mais throughput, aumente gradualmente `max-instances` e `concurrency` monitorando latência, erros e custo.

## Rede e conectividade no Cloud Run

A aplicação precisa sair para internet para:

- APIs da AWS
- APIs da Azure
- Dataset remoto do CompareCloud (quando disponível)

Se sua organização usa regras restritivas de egress, valide firewall/NAT/VPC connector antes de colocar em produção.

## Atualizando o serviço (novas versões)

1. Gere uma nova imagem.
```bash
gcloud builds submit \
  --tag $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$SERVICE_NAME:v2
```

2. Publique a nova imagem.
```bash
gcloud run deploy $SERVICE_NAME \
  --image $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$SERVICE_NAME:v2 \
  --region $REGION
```

## Troubleshooting rápido

1. `AWS scan failed: EndpointConnectionError`
- Normalmente é bloqueio de rede para endpoints AWS ou credenciais inválidas.

2. `Azure scan failed: ClientAuthenticationError`
- Credenciais da service principal inválidas ou bloqueio de rede para `login.microsoftonline.com`.

3. Timeout em scans longos
- Cloud Run tem timeout padrão de 300s e máximo de 3600s para serviços HTTP.
- Se necessário, aumente `--timeout` até 3600.

4. Equivalência vazia em alguns itens
- Nem todo serviço possui mapeamento direto no dataset atual do CompareCloud.

## Próximos passos recomendados para produção

1. Persistir relatórios em GCS para suportar escalabilidade horizontal.
2. Adicionar autenticação de usuário na UI (IAP/OAuth/Identity Aware Proxy).
3. Registrar logs estruturados com correlação por `request_id`/trace.
4. Adicionar fila/background job para scans muito longos.

## Licença e terceiros

- Projeto de equivalência base: `ilyas-it83/CloudComparer` (MIT).
- Detalhes em: `THIRD_PARTY_NOTICES.md`.
