from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .aws_inventory import dt_to_br_str


AZURE_SHEET_TO_COMPARECLOUD_AZURE_NAMES = {
    "Azure_VirtualMachines": ["Azure Virtual Machine"],
    "Azure_VirtualNetworks": ["Azure Virtual Network"],
    "Azure_PublicIPAddresses": ["Public IP Address", "Azure Public IP addresses"],
    "Azure_StorageAccounts": ["Azure Blob Storage", "Storage Account"],
    "Azure_WebApps": ["Web Apps", "Azure App Service"],
    "Azure_FunctionApps": ["Azure Functions"],
    "Azure_AKS_Clusters": ["Azure Kubernetes Service (AKS)"],
    "Azure_ContainerRegistries": ["Azure Container Registry"],
    "Azure_ContainerInstances": ["Azure Container Instances"],
    "Azure_SQLServers": ["Azure SQL Database", "Azure SQL Managed Instance"],
    "Azure_CosmosDB_Accounts": ["Azure Cosmos DB"],
    "Azure_ResourceGroups": ["Azure Resource Manager"],
}


def make_credential(tenant_id, client_id, client_secret):
    if not tenant_id or not client_id or not client_secret:
        raise ValueError("Azure Tenant ID, Client ID, and Client Secret are required for runtime authentication.")
    from azure.identity import ClientSecretCredential

    return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)


def _safe_iter(iterable_factory):
    try:
        return list(iterable_factory())
    except Exception:
        return []


def _collect_subscription_resources(credential, subscription):
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient
    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.mgmt.cosmosdb import CosmosDBManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.sql import SqlManagementClient
    from azure.mgmt.storage import StorageManagementClient
    from azure.mgmt.web import WebSiteManagementClient

    sub_id = subscription["SubscriptionId"]
    resource = ResourceManagementClient(credential=credential, subscription_id=sub_id)
    compute = ComputeManagementClient(credential=credential, subscription_id=sub_id)
    network = NetworkManagementClient(credential=credential, subscription_id=sub_id)
    storage = StorageManagementClient(credential=credential, subscription_id=sub_id)
    web = WebSiteManagementClient(credential=credential, subscription_id=sub_id)
    aks = ContainerServiceClient(credential=credential, subscription_id=sub_id)
    acr = ContainerRegistryManagementClient(credential=credential, subscription_id=sub_id)
    aci = ContainerInstanceManagementClient(credential=credential, subscription_id=sub_id)
    cosmos = CosmosDBManagementClient(credential=credential, subscription_id=sub_id)
    sql = SqlManagementClient(credential=credential, subscription_id=sub_id)

    results = {
        "Azure_ResourceGroups": [],
        "Azure_Resources": [],
        "Azure_VirtualMachines": [],
        "Azure_VirtualNetworks": [],
        "Azure_PublicIPAddresses": [],
        "Azure_StorageAccounts": [],
        "Azure_WebApps": [],
        "Azure_FunctionApps": [],
        "Azure_AKS_Clusters": [],
        "Azure_ContainerRegistries": [],
        "Azure_ContainerInstances": [],
        "Azure_SQLServers": [],
        "Azure_CosmosDB_Accounts": [],
    }

    for group in _safe_iter(lambda: resource.resource_groups.list()):
        results["Azure_ResourceGroups"].append({
            "SubscriptionId": sub_id,
            "Name": group.name,
            "Location": group.location,
            "ProvisioningState": getattr(group, "provisioning_state", None),
        })

    for res in _safe_iter(lambda: resource.resources.list()):
        results["Azure_Resources"].append({
            "SubscriptionId": sub_id,
            "Name": res.name,
            "Type": res.type,
            "Location": res.location,
            "ResourceGroup": getattr(res, "resource_group", None),
            "Id": res.id,
        })

    for vm in _safe_iter(lambda: compute.virtual_machines.list_all()):
        results["Azure_VirtualMachines"].append({
            "SubscriptionId": sub_id,
            "Name": vm.name,
            "Location": vm.location,
            "VmSize": (getattr(vm, "hardware_profile", None) or {}).get("vm_size")
            if isinstance(getattr(vm, "hardware_profile", None), dict)
            else getattr(getattr(vm, "hardware_profile", None), "vm_size", None),
            "ProvisioningState": getattr(getattr(vm, "provisioning_state", None), "value", None)
            or getattr(vm, "provisioning_state", None),
            "Id": vm.id,
        })

    for vnet in _safe_iter(lambda: network.virtual_networks.list_all()):
        addr_space = getattr(getattr(vnet, "address_space", None), "address_prefixes", None) or []
        results["Azure_VirtualNetworks"].append({
            "SubscriptionId": sub_id,
            "Name": vnet.name,
            "Location": vnet.location,
            "AddressSpace": [prefix for prefix in addr_space],
            "Id": vnet.id,
        })

    for ip in _safe_iter(lambda: network.public_ip_addresses.list_all()):
        results["Azure_PublicIPAddresses"].append({
            "SubscriptionId": sub_id,
            "Name": ip.name,
            "Location": ip.location,
            "IpAddress": getattr(ip, "ip_address", None),
            "AllocationMethod": getattr(getattr(ip, "public_ip_allocation_method", None), "value", None)
            or getattr(ip, "public_ip_allocation_method", None),
            "Sku": getattr(getattr(ip, "sku", None), "name", None),
            "Id": ip.id,
        })

    for account in _safe_iter(lambda: storage.storage_accounts.list()):
        results["Azure_StorageAccounts"].append({
            "SubscriptionId": sub_id,
            "Name": account.name,
            "Location": account.location,
            "Kind": account.kind,
            "Sku": getattr(getattr(account, "sku", None), "name", None),
            "Id": account.id,
        })

    for app in _safe_iter(lambda: web.web_apps.list()):
        kind = (app.kind or "").lower()
        row = {
            "SubscriptionId": sub_id,
            "Name": app.name,
            "Location": app.location,
            "Kind": app.kind,
            "State": getattr(app, "state", None),
            "DefaultHostName": getattr(app, "default_host_name", None),
            "Id": app.id,
        }
        if "functionapp" in kind:
            results["Azure_FunctionApps"].append(row)
        else:
            results["Azure_WebApps"].append(row)

    for cluster in _safe_iter(lambda: aks.managed_clusters.list()):
        agent_profiles = getattr(cluster, "agent_pool_profiles", None) or []
        desired_nodes = 0
        for profile in agent_profiles:
            desired_nodes += int(getattr(profile, "count", 0) or 0)
        results["Azure_AKS_Clusters"].append({
            "SubscriptionId": sub_id,
            "Name": cluster.name,
            "Location": cluster.location,
            "KubernetesVersion": getattr(cluster, "kubernetes_version", None),
            "ProvisioningState": getattr(cluster, "provisioning_state", None),
            "NodePools": len(agent_profiles),
            "DesiredNodesTotal": desired_nodes,
            "DnsPrefix": getattr(cluster, "dns_prefix", None),
            "Id": cluster.id,
        })

    for registry in _safe_iter(lambda: acr.registries.list()):
        results["Azure_ContainerRegistries"].append({
            "SubscriptionId": sub_id,
            "Name": registry.name,
            "Location": registry.location,
            "Sku": getattr(getattr(registry, "sku", None), "name", None),
            "LoginServer": getattr(registry, "login_server", None),
            "Id": registry.id,
        })

    for group in _safe_iter(lambda: aci.container_groups.list()):
        containers = getattr(group, "containers", None) or []
        results["Azure_ContainerInstances"].append({
            "SubscriptionId": sub_id,
            "Name": group.name,
            "Location": group.location,
            "ContainerCount": len(containers),
            "ProvisioningState": getattr(group, "provisioning_state", None),
            "OsType": getattr(getattr(group, "os_type", None), "value", None) or getattr(group, "os_type", None),
            "IpAddress": getattr(getattr(group, "ip_address", None), "ip", None),
            "Id": group.id,
        })

    for server in _safe_iter(lambda: sql.servers.list()):
        results["Azure_SQLServers"].append({
            "SubscriptionId": sub_id,
            "Name": server.name,
            "Location": server.location,
            "Version": getattr(server, "version", None),
            "FQDN": getattr(server, "fully_qualified_domain_name", None),
            "Id": server.id,
        })

    for account in _safe_iter(lambda: cosmos.database_accounts.list()):
        regions = [loc.location_name for loc in (getattr(account, "locations", None) or [])]
        results["Azure_CosmosDB_Accounts"].append({
            "SubscriptionId": sub_id,
            "Name": account.name,
            "Location": account.location,
            "Kind": getattr(account, "kind", None),
            "ConsistencyPolicy": getattr(getattr(account, "consistency_policy", None), "default_consistency_level", None),
            "Regions": regions,
            "Id": account.id,
        })

    return results


def _apply_comparecloud_equivalents(sheets, mapper):
    if not mapper:
        return sheets
    for sheet_name, rows in (sheets or {}).items():
        if not isinstance(rows, list):
            continue
        candidates = AZURE_SHEET_TO_COMPARECLOUD_AZURE_NAMES.get(sheet_name, [])
        if not candidates:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row["Equivalent_GCP"] = mapper.format_equivalents("azure", candidates, "google")
            row["Equivalent_AWS"] = mapper.format_equivalents("azure", candidates, "aws")
            row["Equivalence_Source"] = mapper.source_url
    return sheets


def _merge_results(result_bundles, key):
    out = []
    for bundle in result_bundles:
        out.extend(bundle.get(key, []))
    return out


def collect_azure_inventory(
    tenant_id,
    client_id,
    client_secret,
    subscription_id=None,
    threads=4,
    mapper=None,
):
    from azure.mgmt.subscription import SubscriptionClient

    credential = make_credential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    sub_client = SubscriptionClient(credential=credential)

    subscriptions = []
    for sub in sub_client.subscriptions.list():
        sid = getattr(sub, "subscription_id", None)
        if not sid:
            continue
        if subscription_id and sid != subscription_id:
            continue
        subscriptions.append({
            "SubscriptionId": sid,
            "DisplayName": getattr(sub, "display_name", None),
            "State": getattr(sub, "state", None),
            "TenantId": tenant_id,
        })

    if not subscriptions:
        raise ValueError("No accessible Azure subscriptions were found for the provided credentials.")

    result_bundles = []
    with ThreadPoolExecutor(max_workers=max(1, int(threads))) as executor:
        futures = {
            executor.submit(_collect_subscription_resources, credential, sub): sub["SubscriptionId"]
            for sub in subscriptions
        }
        for future in as_completed(futures):
            result_bundles.append(future.result())

    collected_at = dt_to_br_str(datetime.now(timezone.utc))
    sheets = {
        "META": [{
            "Provider": "azure",
            "CollectedAt": collected_at,
            "TenantId": tenant_id,
            "SubscriptionCount": len(subscriptions),
            "Subscriptions": [sub["SubscriptionId"] for sub in subscriptions],
        }],
        "Azure_Subscriptions": subscriptions,
    }

    keys = [
        "Azure_ResourceGroups",
        "Azure_Resources",
        "Azure_VirtualMachines",
        "Azure_VirtualNetworks",
        "Azure_PublicIPAddresses",
        "Azure_StorageAccounts",
        "Azure_WebApps",
        "Azure_FunctionApps",
        "Azure_AKS_Clusters",
        "Azure_ContainerRegistries",
        "Azure_ContainerInstances",
        "Azure_SQLServers",
        "Azure_CosmosDB_Accounts",
    ]
    for key in keys:
        sheets[key] = _merge_results(result_bundles, key)

    return _apply_comparecloud_equivalents(sheets, mapper)
