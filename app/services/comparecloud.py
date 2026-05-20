import re
import time
from pathlib import Path

import requests
import yaml


PROVIDERS = {"aws", "azure", "google", "ibm", "oracle", "alibaba", "huawei", "tencent"}
COMPARECLOUD_SOURCE_URL = "https://comparecloud.in/"
COMPARECLOUD_DATA_URL = "https://raw.githubusercontent.com/ilyas-it83/CloudComparer/main/_data/cloudservices.yml"
COMPARECLOUD_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "data" / "cloudservices_snapshot.yml"


def _normalize(value):
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _iter_entry_provider_items(entry, provider):
    for service_group in (entry or {}).get("service", []) or []:
        if not isinstance(service_group, dict):
            continue
        for item in service_group.get(provider, []) or []:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            yield {"name": name, "ref": item.get("ref") or item.get("refs") or ""}


class CompareCloudMapper:
    source_url = COMPARECLOUD_SOURCE_URL
    data_url = COMPARECLOUD_DATA_URL
    snapshot_path = COMPARECLOUD_SNAPSHOT_PATH

    def __init__(self, timeout=30, cache_ttl_seconds=86400):
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._loaded_at = 0
        self._entries = []
        self._index = {provider: {} for provider in PROVIDERS}
        self.last_error = None
        self.current_data_source = self.data_url

    @property
    def is_loaded(self):
        return bool(self._entries)

    def _build_index(self):
        self._index = {provider: {} for provider in PROVIDERS}
        for entry in self._entries:
            for provider in PROVIDERS:
                for item in _iter_entry_provider_items(entry, provider):
                    key = _normalize(item["name"])
                    if not key:
                        continue
                    self._index[provider].setdefault(key, []).append(entry)

    def refresh(self, force=False):
        now = time.time()
        is_stale = (now - self._loaded_at) > self.cache_ttl_seconds
        if not force and self._entries and not is_stale:
            return
        try:
            response = requests.get(self.data_url, timeout=self.timeout)
            response.raise_for_status()
            data_text = response.text
            self.current_data_source = self.data_url
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.snapshot_path.exists():
                data_text = self.snapshot_path.read_text(encoding="utf-8")
                self.current_data_source = str(self.snapshot_path)
            elif not self._entries:
                raise
            else:
                return
        data = yaml.safe_load(data_text) or {}
        services = data.get("services") or []
        if not isinstance(services, list):
            raise ValueError("Invalid comparecloud service data format.")
        self._entries = services
        self._build_index()
        self._loaded_at = now

    def _candidate_keys(self, candidate_names):
        keys = []
        for name in candidate_names or []:
            norm = _normalize(name)
            if norm:
                keys.append(norm)
        return keys

    def match(self, source_provider, candidate_names, target_provider):
        source_provider = (source_provider or "").strip().lower()
        target_provider = (target_provider or "").strip().lower()
        if source_provider not in PROVIDERS or target_provider not in PROVIDERS:
            return None

        self.refresh()
        for key in self._candidate_keys(candidate_names):
            for entry in self._index.get(source_provider, {}).get(key, []):
                target_items = list(_iter_entry_provider_items(entry, target_provider))
                if not target_items:
                    continue
                return {
                    "category": (entry or {}).get("category") or "",
                    "subcategory": (entry or {}).get("subcategory") or "",
                    "services": [item["name"] for item in target_items],
                    "refs": [item["ref"] for item in target_items if item.get("ref")],
                }
        return None

    def format_equivalents(self, source_provider, candidate_names, target_provider):
        if not candidate_names:
            return ""
        match = self.match(source_provider, candidate_names, target_provider)
        if not match:
            return ""
        return " | ".join(match["services"])
