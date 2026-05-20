# SBACloudCompare

Aplicacao web para inventario de recursos em cloud e comparacao de servicos entre provedores, com foco inicial em AWS e Azure.

## Visao geral

- Leitura de recursos AWS com exportacao em Excel multi-aba.
- Leitura de recursos Azure com exportacao em Excel multi-aba.
- Equivalencia entre provedores usando a base do CompareCloud.
- Interface web com explicacao dos campos e download do arquivo final.
- Estrutura pronta para deploy em Cloud Run.

## Como a autenticacao funciona

- AWS: voce informa `Access Key ID`, `Secret Access Key` e opcionalmente `Session Token` no formulario.
- Azure: voce informa `Tenant ID`, `Client ID` e `Client Secret` da service principal.
- As credenciais sao usadas apenas na execucao da leitura.
- As credenciais nao sao gravadas em `.env`, nao vao para arquivo local e nao ficam hardcoded no codigo.

## Fonte de equivalencia (CompareCloud)

- Site: `https://comparecloud.in/`
- Dataset remoto: `https://raw.githubusercontent.com/ilyas-it83/CloudComparer/main/_data/cloudservices.yml`
- Fallback local: `app/data/cloudservices_snapshot.yml`

Se o dataset remoto estiver indisponivel, a aplicacao usa o snapshot local automaticamente.

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

3. Instale dependencias.
```bash
pip install -r requirements.txt
```

4. Rode a aplicacao.
```bash
uvicorn app.main:app --reload --port 8080
```

5. Teste endpoints basicos.
```bash
curl http://localhost:8080/healthz
```

6. Abra no navegador.
```text
http://localhost:8080
```

## Deploy no Cloud Run (guia completo)

### 1) Pre-requisitos GCP

1. Projeto GCP criado e com billing ativo.
2. `gcloud` instalado e atualizado.
3. Permissoes IAM para deploy. Exemplo minimo:
- `roles/run.admin`
- `roles/iam.serviceAccountUser`
- `roles/cloudbuild.builds.editor`
- `roles/artifactregistry.admin` (ou papeis equivalentes para usar repositorio existente)

### 2) Login e configuracao inicial

1. Login no Google Cloud.
```bash
gcloud auth login
```

2. Defina variaveis de ambiente (Linux/macOS).
```bash
export PROJECT_ID="seu-projeto-gcp"
export REGION="us-central1"
export SERVICE_NAME="sba-cloud-compare"
export REPO_NAME="cloud-run-images"
```

3. Defina variaveis de ambiente (Windows PowerShell).
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

### 3) Habilite APIs necessarias

```bash
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable artifactregistry.googleapis.com
```

### 4) Crie repositorio no Artifact Registry (uma vez)

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

1. Obtenha URL do servico.
```bash
gcloud run services describe $SERVICE_NAME --region $REGION --format='value(status.url)'
```

2. Teste healthcheck.
```bash
curl https://SUA_URL/healthz
```

3. Abra a URL no navegador e execute uma leitura AWS ou Azure.

## Por que `max-instances=1` e `concurrency=1` neste momento

- O download do relatorio usa cache em memoria local da instancia.
- Se multiplas instancias forem usadas, a requisicao de download pode cair em outra instancia e retornar "Report not found or expired".
- Para escalar horizontalmente sem esse risco, o ideal e salvar relatorios em storage externo (ex.: GCS + chave de download).

## Rede e conectividade no Cloud Run

A aplicacao precisa sair para internet para:

- APIs da AWS
- APIs da Azure
- Dataset remoto do CompareCloud (quando disponivel)

Se sua organizacao usa regras restritivas de egress, valide firewall/NAT/VPC connector antes de colocar em producao.

## Atualizando o servico (novas versoes)

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

## Troubleshooting rapido

1. `AWS scan failed: EndpointConnectionError`
- Normalmente e bloqueio de rede para endpoints AWS ou credenciais invalidas.

2. `Azure scan failed: ClientAuthenticationError`
- Credenciais da service principal invalidas ou bloqueio de rede para `login.microsoftonline.com`.

3. Timeout em scans longos
- Cloud Run tem timeout padrao de 300s e maximo de 3600s para servicos HTTP.
- Se necessario, aumente `--timeout` ate 3600.

4. Equivalencia vazia em alguns itens
- Nem todo servico possui mapeamento direto no dataset atual do CompareCloud.

## Proximos passos recomendados para producao

1. Persistir relatorios em GCS para suportar escalabilidade horizontal.
2. Adicionar autenticacao de usuario na UI (IAP/OAuth/Identity Aware Proxy).
3. Registrar logs estruturados com correlacao por `scan_id`.
4. Adicionar fila/background job para scans muito longos.

## Licenca e terceiros

- Projeto de equivalencia base: `ilyas-it83/CloudComparer` (MIT).
- Detalhes em: `THIRD_PARTY_NOTICES.md`.
