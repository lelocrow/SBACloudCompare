import re
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .services.aws_inventory import collect_aws_inventory, sheets_to_workbook_bytes
from .services.azure_inventory import collect_azure_inventory
from .services.comparecloud import CompareCloudMapper


APP_TTL_SECONDS = 3600
SCAN_CACHE = {}
mapper = CompareCloudMapper()

app = FastAPI(title="SBA Cloud Compare", version="0.1.0")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _now_utc():
    return datetime.now(timezone.utc)


def _sanitize_file_stem(name, fallback):
    stem = (name or "").strip()
    if not stem:
        stem = fallback
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem)
    return stem[:80].strip("._-") or fallback


def _cleanup_cache():
    cutoff = _now_utc().timestamp() - APP_TTL_SECONDS
    stale_keys = [key for key, value in SCAN_CACHE.items() if value["created_at"] < cutoff]
    for key in stale_keys:
        SCAN_CACHE.pop(key, None)


def _store_report(provider, project_name, sheets, workbook_bytes):
    _cleanup_cache()
    scan_id = str(uuid.uuid4())
    filename = f"{_sanitize_file_stem(project_name, f'{provider}_inventory')}.xlsx"
    sheet_counts = {sheet: len(rows) if isinstance(rows, list) else 0 for sheet, rows in sheets.items()}
    SCAN_CACHE[scan_id] = {
        "provider": provider,
        "filename": filename,
        "bytes": workbook_bytes,
        "sheets": sheet_counts,
        "created_at": _now_utc().timestamp(),
    }
    return scan_id, filename, sheet_counts


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
    default_region: str = Form("us-east-1"),
    home_region: str = Form("us-east-1"),
    threads: int = Form(8),
):
    if not access_key_id.strip() or not secret_access_key.strip():
        raise HTTPException(status_code=400, detail="AWS Access Key ID and Secret Access Key are required.")

    try:
        sheets = collect_aws_inventory(
            access_key_id=access_key_id.strip(),
            secret_access_key=secret_access_key.strip(),
            session_token=session_token.strip() or None,
            default_region=default_region.strip() or "us-east-1",
            home_region=home_region.strip() or "us-east-1",
            threads=max(1, int(threads)),
            mapper=mapper,
        )
        workbook_bytes = sheets_to_workbook_bytes(sheets)
        scan_id, filename, sheet_counts = _store_report(
            provider="aws",
            project_name=project_name or "aws_inventory",
            sheets=sheets,
            workbook_bytes=workbook_bytes,
        )
        return {
            "scan_id": scan_id,
            "provider": "aws",
            "filename": filename,
            "sheet_counts": sheet_counts,
            "mapping_source": mapper.source_url,
            "mapping_data_url": mapper.data_url,
            "mapping_data_source": mapper.current_data_source,
            "mapping_last_error": mapper.last_error,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"AWS scan failed: {type(exc).__name__}: {exc}") from exc


@app.post("/api/scan/azure")
def scan_azure(
    project_name: str = Form(""),
    tenant_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    subscription_id: str = Form(""),
    threads: int = Form(4),
):
    if not tenant_id.strip() or not client_id.strip() or not client_secret.strip():
        raise HTTPException(status_code=400, detail="Azure Tenant ID, Client ID, and Client Secret are required.")

    try:
        sheets = collect_azure_inventory(
            tenant_id=tenant_id.strip(),
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            subscription_id=subscription_id.strip() or None,
            threads=max(1, int(threads)),
            mapper=mapper,
        )
        workbook_bytes = sheets_to_workbook_bytes(sheets)
        scan_id, filename, sheet_counts = _store_report(
            provider="azure",
            project_name=project_name or "azure_inventory",
            sheets=sheets,
            workbook_bytes=workbook_bytes,
        )
        return {
            "scan_id": scan_id,
            "provider": "azure",
            "filename": filename,
            "sheet_counts": sheet_counts,
            "mapping_source": mapper.source_url,
            "mapping_data_url": mapper.data_url,
            "mapping_data_source": mapper.current_data_source,
            "mapping_last_error": mapper.last_error,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Azure scan failed: {type(exc).__name__}: {exc}") from exc


@app.get("/api/download/{scan_id}")
def download_report(scan_id: str):
    payload = SCAN_CACHE.get(scan_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Report not found or expired.")
    headers = {
        "Content-Disposition": f"attachment; filename={payload['filename']}",
        "Cache-Control": "no-store",
    }
    return Response(
        content=payload["bytes"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.exception_handler(HTTPException)
def on_http_exception(_request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
