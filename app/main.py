import logging
import re

from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .services.aws_inventory import collect_aws_inventory, sheets_to_workbook_bytes
from .services.azure_inventory import collect_azure_inventory
from .services.comparecloud import CompareCloudMapper


logger = logging.getLogger(__name__)
mapper = CompareCloudMapper()

app = FastAPI(title="SBA Cloud Compare", version="0.2.0")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Execution settings intentionally kept server-side to simplify the UI.
AWS_DEFAULT_REGION = "us-east-1"
AWS_RESOURCE_EXPLORER_HOME_REGION = "us-east-1"
AWS_SCAN_THREADS = 8
AZURE_SCAN_THREADS = 4


def _sanitize_file_stem(name, fallback):
    stem = (name or "").strip()
    if not stem:
        stem = fallback
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem)
    return stem[:80].strip("._-") or fallback


def _mapping_data_source_label():
    if mapper.current_data_source == mapper.data_url:
        return "remote"
    return "snapshot"


def _public_error_message(provider, exc):
    if isinstance(exc, ValueError):
        return str(exc)

    if provider == "aws":
        if isinstance(exc, ClientError):
            return "Falha de autenticação ou permissão na AWS. Verifique Access Key, Secret Key e permissões IAM."
        if isinstance(exc, EndpointConnectionError):
            return "Falha de conectividade com endpoints da AWS. Verifique rede, egress e DNS."
        return "Falha ao executar a leitura AWS. Verifique conectividade e permissões da conta."

    exc_name = type(exc).__name__
    if exc_name in {"ClientAuthenticationError", "CredentialUnavailableError"}:
        return "Falha de autenticação no Azure. Verifique Tenant ID, Client ID, Client Secret e permissões da service principal."
    return "Falha ao executar a leitura Azure. Verifique conectividade e permissões da assinatura."


def _build_excel_response(provider, project_name, sheets, workbook_bytes):
    filename = f"{_sanitize_file_stem(project_name, f'{provider}_inventory')}.xlsx"
    warnings_count = len(sheets.get("WARNINGS", [])) if isinstance(sheets.get("WARNINGS"), list) else 0
    sheet_count = len([name for name, rows in (sheets or {}).items() if isinstance(rows, list)])
    total_rows = sum(len(rows) for rows in (sheets or {}).values() if isinstance(rows, list))

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-SBA-Provider": provider,
        "X-SBA-Warnings-Count": str(warnings_count),
        "X-SBA-Sheet-Count": str(sheet_count),
        "X-SBA-Total-Rows": str(total_rows),
        "X-SBA-Mapping-Source": mapper.source_url,
        "X-SBA-Mapping-Data-Source": _mapping_data_source_label(),
    }
    return Response(
        content=workbook_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "mapping_source": mapper.source_url,
            "data_source": mapper.data_url,
        },
    )


@app.post("/api/scan/aws")
def scan_aws(
    project_name: str = Form(""),
    access_key_id: str = Form(...),
    secret_access_key: str = Form(...),
    session_token: str = Form(""),
):
    if not access_key_id.strip() or not secret_access_key.strip():
        raise HTTPException(status_code=400, detail="AWS Access Key ID e Secret Access Key são obrigatórios.")

    try:
        sheets = collect_aws_inventory(
            access_key_id=access_key_id.strip(),
            secret_access_key=secret_access_key.strip(),
            session_token=session_token.strip() or None,
            default_region=AWS_DEFAULT_REGION,
            home_region=AWS_RESOURCE_EXPLORER_HOME_REGION,
            threads=AWS_SCAN_THREADS,
            mapper=mapper,
        )
        workbook_bytes = sheets_to_workbook_bytes(sheets)
        return _build_excel_response(
            provider="aws",
            project_name=project_name or "aws_inventory",
            sheets=sheets,
            workbook_bytes=workbook_bytes,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Falha na leitura AWS: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail=_public_error_message("aws", exc)) from exc


@app.post("/api/scan/azure")
def scan_azure(
    project_name: str = Form(""),
    tenant_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    subscription_id: str = Form(""),
):
    if not tenant_id.strip() or not client_id.strip() or not client_secret.strip():
        raise HTTPException(status_code=400, detail="Azure Tenant ID, Client ID e Client Secret são obrigatórios.")

    try:
        sheets = collect_azure_inventory(
            tenant_id=tenant_id.strip(),
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            subscription_id=subscription_id.strip() or None,
            threads=AZURE_SCAN_THREADS,
            mapper=mapper,
        )
        workbook_bytes = sheets_to_workbook_bytes(sheets)
        return _build_excel_response(
            provider="azure",
            project_name=project_name or "azure_inventory",
            sheets=sheets,
            workbook_bytes=workbook_bytes,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Falha na leitura Azure: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail=_public_error_message("azure", exc)) from exc


@app.exception_handler(HTTPException)
def on_http_exception(_request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
