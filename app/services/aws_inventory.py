#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from io import BytesIO

import boto3
import pandas as pd
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError, EndpointConnectionError

def resolve_br_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        return timezone(timedelta(hours=-3))

BR_TZ = resolve_br_tz()


def _trim_error_message(exc, limit=240):
    msg = str(exc or "").replace("\n", " ").strip()
    return msg[:limit]


def _warn(warnings, service, region, operation, exc):
    if warnings is None:
        return
    warnings.append(
        {
            "Provider": "aws",
            "Service": service,
            "Region": region or "unknown",
            "Operation": operation,
            "ErrorType": type(exc).__name__,
            "ErrorMessage": _trim_error_message(exc),
            "CapturedAt": dt_to_br_str(datetime.now(timezone.utc)),
        }
    )

def dt_to_br_str(dt):
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BR_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return dt

def to_rows(objlist):
    rows = []
    for o in objlist or []:
        r = {}
        for k, v in (o or {}).items():
            if isinstance(v, datetime):
                r[k] = dt_to_br_str(v)
            elif isinstance(v, (dict, list, tuple, set)):
                r[k] = json.dumps(v, ensure_ascii=False, default=str)
            else:
                r[k] = v
        rows.append(r)
    return rows

def write_sheet(writer, name, rows):
    if rows is None:
        rows = []
    df = pd.DataFrame(to_rows(rows))
    if df.shape[1] == 0:
        df = pd.DataFrame({"info": ["(vazio)"]})
    sheet_name = name[:31] if len(name) > 31 else name
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(0, df.shape[0]), max(0, df.shape[1] - 1))
    for i, col in enumerate(df.columns):
        try:
            maxlen = max((df[col].astype(str).str.len().max() or 0), len(str(col)))
        except Exception:
            maxlen = len(str(col))
        maxlen = min(maxlen + 2, 60)
        ws.set_column(i, i, maxlen)

def ensure_parent(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

def make_session(access_key_id, secret_access_key, session_token=None, default_region="us-east-1"):
    if not access_key_id or not secret_access_key:
        raise ValueError("AWS Access Key ID and Secret Access Key are required for runtime authentication.")
    creds = Credentials(access_key_id, secret_access_key, session_token)
    return boto3.Session(
        region_name=default_region or "us-east-1",
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        aws_session_token=creds.token,
    )

def available_regions(session):
    try:
        ec2 = session.client("ec2", region_name="us-east-1")
        resp = ec2.describe_regions(AllRegions=True)
        return [r["RegionName"] for r in resp.get("Regions", [])]
    except Exception:
        return session.get_available_regions("ec2")

def is_region_reachable(session, region):
    try:
        ec2 = session.client("ec2", region_name=region, config=Config(retries={"max_attempts": 2}))
        ec2.describe_account_attributes()
        return True
    except Exception:
        return False

def collect_resource_explorer(session, home_region="us-east-1"):
    items = []
    status = "unavailable"
    try:
        rex = session.client("resource-explorer-2", region_name=home_region)
        views = rex.list_views(MaxResults=50).get("Views", [])
        view_arn = None
        for v in views:
            if v.get("DefaultView"):
                view_arn = v["ViewArn"]
                break
        if not view_arn and views:
            view_arn = views[0]["ViewArn"]
        if not view_arn:
            return items, "disabled"
        next_token = None
        while True:
            kw = {"ViewArn": view_arn, "MaxResults": 1000}
            if next_token:
                kw["NextToken"] = next_token
            resp = rex.search(**kw)
            for r in resp.get("Resources", []):
                items.append({
                    "Region": r.get("Region") or "global",
                    "Arn": r.get("Arn"),
                    "Service": r.get("Service"),
                    "LastReportedAt": r.get("LastReportedAt"),
                    "ResourceType": r.get("ResourceType"),
                    "Tags": r.get("Tags"),
                    "Equivalente_GCP": ""
                })
            next_token = resp.get("NextToken")
            if not next_token:
                break
        status = "ok"
    except EndpointConnectionError:
        status = "disabled"
    except ClientError:
        status = "disabled"
    except Exception:
        status = "disabled"
    return items, status

def s3_bucket_size_via_cw(session, bucket, bucket_region):
    cw = session.client("cloudwatch", region_name="us-east-1")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=3)
    dims = [{"Name": "BucketName", "Value": bucket}, {"Name": "StorageType", "Value": "StandardStorage"}]
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="BucketSizeBytes",
            Dimensions=dims,
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Average"],
        )
        dps = resp.get("Datapoints", [])
        if not dps:
            return None
        latest = sorted(dps, key=lambda x: x["Timestamp"])[-1]
        return latest.get("Average")
    except Exception:
        return None

def s3_bucket_size_via_listing(session, bucket, bucket_region):
    try:
        cli = session.client("s3", region_name=bucket_region if bucket_region and bucket_region != "unknown" else None)
    except Exception:
        cli = session.client("s3")
    total = 0
    try:
        paginator = cli.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []) or []:
                total += int(obj.get("Size") or 0)
        return total
    except Exception:
        return None

def collect_s3(session, warnings=None):
    cli = session.client("s3")
    out = []
    try:
        resp = cli.list_buckets()
        for b in resp.get("Buckets", []):
            name = b.get("Name")
            try:
                loc = cli.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
            except Exception as exc:
                loc = "unknown"
                _warn(warnings, "s3", "global", "get_bucket_location", exc)
            size_bytes = s3_bucket_size_via_cw(session, name, loc)
            if size_bytes is None:
                size_bytes = s3_bucket_size_via_listing(session, name, loc)
            if size_bytes is not None:
                gb = round(size_bytes / (1024**3), 2)
                eq = f"Cloud Storage (~{gb} GB)"
            else:
                eq = "Cloud Storage (tamanho não disponível)"
            out.append({
                "Region": loc,
                "Bucket": name,
                "CreateDate": b.get("CreationDate"),
                "SizeBytes": size_bytes,
                "Equivalente_GCP": eq
            })
    except Exception as exc:
        _warn(warnings, "s3", "global", "list_buckets", exc)
    return out

def collect_cloudfront(session, warnings=None):
    out = []
    try:
        cli = session.client("cloudfront")
        paginator = cli.get_paginator("list_distributions")
        for page in paginator.paginate():
            for d in (page.get("DistributionList") or {}).get("Items", []):
                out.append({
                    "Region": "global",
                    "Id": d.get("Id"),
                    "ARN": d.get("ARN"),
                    "Status": d.get("Status"),
                    "DomainName": d.get("DomainName"),
                    "Enabled": d.get("Enabled"),
                    "Equivalente_GCP": "Cloud CDN"
                })
    except Exception as exc:
        _warn(warnings, "cloudfront", "global", "list_distributions", exc)
    return out

def collect_route53(session, warnings=None):
    out = []
    try:
        cli = session.client("route53")
        paginator = cli.get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            for z in page.get("HostedZones", []):
                out.append({
                    "Region": "global",
                    "Id": z.get("Id"),
                    "Name": z.get("Name"),
                    "PrivateZone": z.get("Config", {}).get("PrivateZone"),
                    "ResourceRecordSetCount": z.get("ResourceRecordSetCount"),
                    "Equivalente_GCP": "Cloud DNS"
                })
    except Exception as exc:
        _warn(warnings, "route53", "global", "list_hosted_zones", exc)
    return out

def collect_iam(session, warnings=None):
    out_users, out_roles, out_policies = [], [], []
    try:
        cli = session.client("iam")
        paginator = cli.get_paginator("list_users")
        for page in paginator.paginate():
            for u in page.get("Users", []):
                out_users.append({
                    "Region": "global",
                    "UserName": u.get("UserName"),
                    "UserId": u.get("UserId"),
                    "CreateDate": u.get("CreateDate"),
                    "Arn": u.get("Arn"),
                    "Equivalente_GCP": "IAM (usuário/identity)"
                })
        paginator = cli.get_paginator("list_roles")
        for page in paginator.paginate():
            for r in page.get("Roles", []):
                out_roles.append({
                    "Region": "global",
                    "RoleName": r.get("RoleName"),
                    "RoleId": r.get("RoleId"),
                    "CreateDate": r.get("CreateDate"),
                    "Arn": r.get("Arn"),
                    "AssumeRolePolicyDocument": r.get("AssumeRolePolicyDocument"),
                    "Equivalente_GCP": "IAM (service account/role)"
                })
        paginator = cli.get_paginator("list_policies")
        for page in paginator.paginate(Scope="All"):
            for p in page.get("Policies", []):
                out_policies.append({
                    "Region": "global",
                    "PolicyName": p.get("PolicyName"),
                    "PolicyId": p.get("PolicyId"),
                    "Arn": p.get("Arn"),
                    "DefaultVersionId": p.get("DefaultVersionId"),
                    "AttachmentCount": p.get("AttachmentCount"),
                    "Equivalente_GCP": "IAM (role/policy binding)"
                })
    except Exception as exc:
        _warn(warnings, "iam", "global", "list_users_roles_policies", exc)
    return out_users, out_roles, out_policies

def describe_instance_types(session, region, instance_types):
    if not instance_types:
        return {}
    cli = session.client("ec2", region_name=region)
    out = {}
    batch = list(instance_types)
    for i in range(0, len(batch), 100):
        chunk = batch[i:i+100]
        try:
            resp = cli.describe_instance_types(InstanceTypes=chunk)
            for it in resp.get("InstanceTypes", []):
                out[it["InstanceType"]] = {
                    "VCpuInfo": it.get("VCpuInfo", {}),
                    "MemoryInfo": it.get("MemoryInfo", {})
                }
        except Exception:
            continue
    return out

def collect_ec2(session, region, warnings=None):
    cli = session.client("ec2", region_name=region)
    results = {"Instances": [], "Volumes": [], "Snapshots": [], "SecurityGroups": [], "Vpcs": [], "Subnets": [], "RouteTables": [], "Addresses": []}
    instance_types_seen = set()
    try:
        paginator = cli.get_paginator("describe_instances")
        for page in paginator.paginate():
            for res in page.get("Reservations", []):
                for i in res.get("Instances", []):
                    itype = i.get("InstanceType")
                    if itype:
                        instance_types_seen.add(itype)
                    results["Instances"].append({
                        "Region": region,
                        "InstanceId": i.get("InstanceId"),
                        "InstanceType": itype,
                        "State": i.get("State", {}).get("Name"),
                        "VpcId": i.get("VpcId"),
                        "SubnetId": i.get("SubnetId"),
                        "PrivateIp": i.get("PrivateIpAddress"),
                        "PublicIp": i.get("PublicIpAddress"),
                        "LaunchTime": i.get("LaunchTime"),
                        "Tags": i.get("Tags")
                    })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_instances", exc)
    itype_details = describe_instance_types(session, region, instance_types_seen)
    for inst in results["Instances"]:
        it = inst.get("InstanceType")
        vc = None
        mem = None
        if it and it in itype_details:
            vc = itype_details[it].get("VCpuInfo", {}).get("DefaultVCpus")
            mem_mib = itype_details[it].get("MemoryInfo", {}).get("SizeInMiB")
            if mem_mib is not None:
                mem = round(mem_mib / 1024, 2)
        if vc is not None and mem is not None:
            inst["Equivalente_GCP"] = f"Compute Engine ({vc} vCPUs, {mem} GB)"
        else:
            inst["Equivalente_GCP"] = "Compute Engine"
    try:
        paginator = cli.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for v in page.get("Volumes", []):
                size = v.get("Size")
                eq = f"Persistent Disk ({size} GiB)" if size is not None else "Persistent Disk"
                results["Volumes"].append({
                    "Region": region,
                    "VolumeId": v.get("VolumeId"),
                    "SizeGiB": size,
                    "State": v.get("State"),
                    "Encrypted": v.get("Encrypted"),
                    "MultiAttach": v.get("MultiAttachEnabled"),
                    "Iops": v.get("Iops"),
                    "Throughput": v.get("Throughput"),
                    "Type": v.get("VolumeType"),
                    "Tags": v.get("Tags"),
                    "Equivalente_GCP": eq
                })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_volumes", exc)
    try:
        paginator = cli.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for s in page.get("Snapshots", []):
                results["Snapshots"].append({
                    "Region": region,
                    "SnapshotId": s.get("SnapshotId"),
                    "VolumeId": s.get("VolumeId"),
                    "StartTime": s.get("StartTime"),
                    "State": s.get("State"),
                    "Encrypted": s.get("Encrypted"),
                    "Tags": s.get("Tags"),
                    "Equivalente_GCP": "Disk snapshot"
                })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_snapshots", exc)
    try:
        sgs = cli.describe_security_groups().get("SecurityGroups", [])
        for sg in sgs:
            results["SecurityGroups"].append({
                "Region": region,
                "GroupId": sg.get("GroupId"),
                "GroupName": sg.get("GroupName"),
                "VpcId": sg.get("VpcId"),
                "Description": sg.get("Description"),
                "InboundCount": len(sg.get("IpPermissions", [])),
                "OutboundCount": len(sg.get("IpPermissionsEgress", [])),
                "Tags": sg.get("Tags"),
                "Equivalente_GCP": "VPC firewall rule"
            })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_security_groups", exc)
    try:
        vpcs = cli.describe_vpcs().get("Vpcs", [])
        for vpc in vpcs:
            results["Vpcs"].append({
                "Region": region,
                "VpcId": vpc.get("VpcId"),
                "CidrBlock": vpc.get("CidrBlock"),
                "IsDefault": vpc.get("IsDefault"),
                "Tags": vpc.get("Tags"),
                "Equivalente_GCP": "VPC Network"
            })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_vpcs", exc)
    try:
        subnets = cli.describe_subnets().get("Subnets", [])
        for sn in subnets:
            results["Subnets"].append({
                "Region": region,
                "SubnetId": sn.get("SubnetId"),
                "VpcId": sn.get("VpcId"),
                "CidrBlock": sn.get("CidrBlock"),
                "AvailabilityZone": sn.get("AvailabilityZone"),
                "Tags": sn.get("Tags"),
                "Equivalente_GCP": "VPC Subnet"
            })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_subnets", exc)
    try:
        rts = cli.describe_route_tables().get("RouteTables", [])
        for rt in rts:
            results["RouteTables"].append({
                "Region": region,
                "RouteTableId": rt.get("RouteTableId"),
                "VpcId": rt.get("VpcId"),
                "Associations": len(rt.get("Associations", [])),
                "Routes": len(rt.get("Routes", [])),
                "Tags": rt.get("Tags"),
                "Equivalente_GCP": "VPC Route"
            })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_route_tables", exc)
    try:
        addrs = cli.describe_addresses().get("Addresses", [])
        for a in addrs:
            results["Addresses"].append({
                "Region": region,
                "PublicIp": a.get("PublicIp"),
                "AllocationId": a.get("AllocationId"),
                "AssociationId": a.get("AssociationId"),
                "InstanceId": a.get("InstanceId"),
                "NetworkInterfaceId": a.get("NetworkInterfaceId"),
                "PrivateIpAddress": a.get("PrivateIpAddress"),
                "Equivalente_GCP": "Static external IP"
            })
    except Exception as exc:
        _warn(warnings, "ec2", region, "describe_addresses", exc)
    return results

def collect_elb(session, region, warnings=None):
    out_v2, out_classic = [], []
    try:
        elbv2 = session.client("elbv2", region_name=region)
        paginator = elbv2.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                out_v2.append({
                    "Region": region,
                    "LoadBalancerArn": lb.get("LoadBalancerArn"),
                    "DNSName": lb.get("DNSName"),
                    "Type": lb.get("Type"),
                    "Scheme": lb.get("Scheme"),
                    "State": lb.get("State", {}).get("Code"),
                    "VpcId": lb.get("VpcId"),
                    "Equivalente_GCP": "Cloud Load Balancing"
                })
    except Exception as exc:
        _warn(warnings, "elbv2", region, "describe_load_balancers", exc)
    try:
        elb = session.client("elb", region_name=region)
        paginator = elb.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                out_classic.append({
                    "Region": region,
                    "LoadBalancerName": lb.get("LoadBalancerName"),
                    "DNSName": lb.get("DNSName"),
                    "Scheme": lb.get("Scheme", "internet-facing"),
                    "Instances": len(lb.get("Instances", [])),
                    "VPCId": lb.get("VPCId"),
                    "Equivalente_GCP": "Cloud Load Balancing"
                })
    except Exception as exc:
        _warn(warnings, "elb", region, "describe_load_balancers", exc)
    return out_v2, out_classic

def docdb_cluster_size_via_cw(session, region, cluster_id):
    try:
        cw = session.client("cloudwatch", region_name=region)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=3)
        resp = cw.get_metric_statistics(
            Namespace="AWS/DocDB",
            MetricName="VolumeBytesUsed",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Average"],
        )
        dps = resp.get("Datapoints", [])
        if not dps:
            return None
        latest = sorted(dps, key=lambda x: x["Timestamp"])[-1]
        return int(latest.get("Average") or 0)
    except Exception:
        return None

def collect_rds(session, region, warnings=None):
    out_instances, out_clusters = [], []
    try:
        cli = session.client("rds", region_name=region)
        paginator = cli.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page.get("DBInstances", []):
                alloc = db.get("AllocatedStorage")
                eng = (db.get("Engine") or "").lower()
                if eng.startswith("sqlserver"):
                    eq = f"Cloud SQL – SQL Server ({alloc} GiB)" if alloc else "Cloud SQL – SQL Server"
                elif eng in ("mysql", "mariadb"):
                    eq = f"Cloud SQL – MySQL ({alloc} GiB)" if alloc else "Cloud SQL – MySQL"
                elif eng.startswith("postgres"):
                    eq = f"Cloud SQL – PostgreSQL ({alloc} GiB)" if alloc else "Cloud SQL – PostgreSQL"
                elif eng in ("docdb", "docdb-elastic"):
                    eq = "MongoDB Atlas (GCP) / MongoDB em GCE/GKE"
                elif eng.startswith("aurora"):
                    eq = "AlloyDB / Cloud SQL (conforme engine do Aurora)"
                else:
                    eq = f"Cloud SQL ({eng})"
                out_instances.append({
                    "Region": region,
                    "DBInstanceIdentifier": db.get("DBInstanceIdentifier"),
                    "Engine": db.get("Engine"),
                    "EngineVersion": db.get("EngineVersion"),
                    "DBInstanceClass": db.get("DBInstanceClass"),
                    "MultiAZ": db.get("MultiAZ"),
                    "StorageType": db.get("StorageType"),
                    "AllocatedStorageGiB": alloc,
                    "Status": db.get("DBInstanceStatus"),
                    "Endpoint": (db.get("Endpoint") or {}).get("Address"),
                    "VpcSecurityGroups": [v.get("VpcSecurityGroupId") for v in db.get("VpcSecurityGroups", [])],
                    "Equivalente_GCP": eq
                })
    except Exception as exc:
        _warn(warnings, "rds", region, "describe_db_instances", exc)
    try:
        cli = session.client("rds", region_name=region)
        paginator = cli.get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for c in page.get("DBClusters", []):
                eng = (c.get("Engine") or "").lower()
                size_bytes = None
                if eng in ("docdb", "docdb-elastic"):
                    size_bytes = docdb_cluster_size_via_cw(session, region, c.get("DBClusterIdentifier"))
                    if size_bytes is not None:
                        gb = round(size_bytes / (1024**3), 2)
                        eq = f"MongoDB Atlas (GCP) / MongoDB em GCE/GKE (~{gb} GB)"
                    else:
                        eq = "MongoDB Atlas (GCP) / MongoDB em GCE/GKE"
                elif eng.startswith("aurora"):
                    eq = "AlloyDB / Cloud SQL (conforme engine do Aurora)"
                else:
                    eq = f"Cloud SQL ({eng})"
                out_clusters.append({
                    "Region": region,
                    "DBClusterIdentifier": c.get("DBClusterIdentifier"),
                    "Engine": c.get("Engine"),
                    "EngineMode": c.get("EngineMode"),
                    "Status": c.get("Status"),
                    "Endpoint": c.get("Endpoint"),
                    "ReaderEndpoint": c.get("ReaderEndpoint"),
                    "ClusterStorageBytes_Estimated": size_bytes,
                    "ClusterStorageGB_Estimated": round(size_bytes / (1024**3), 2) if size_bytes else None,
                    "Equivalente_GCP": eq
                })
    except Exception as exc:
        _warn(warnings, "rds", region, "describe_db_clusters", exc)
    return out_instances, out_clusters

def collect_lambda(session, region, warnings=None):
    out = []
    try:
        cli = session.client("lambda", region_name=region)
        paginator = cli.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                mem = fn.get("MemorySize")
                eq = f"Cloud Functions ({mem} MB)" if mem is not None else "Cloud Functions"
                out.append({
                    "Region": region,
                    "FunctionName": fn.get("FunctionName"),
                    "Runtime": fn.get("Runtime"),
                    "MemorySize": mem,
                    "Timeout": fn.get("Timeout"),
                    "LastModified": fn.get("LastModified"),
                    "State": fn.get("State") or "Unknown",
                    "Equivalente_GCP": eq
                })
    except Exception as exc:
        _warn(warnings, "lambda", region, "list_functions", exc)
    return out

def collect_eks(session, region, warnings=None):
    out = []
    out_nodegroups = []
    try:
        cli = session.client("eks", region_name=region)
        paginator = cli.get_paginator("list_clusters")
        for page in paginator.paginate():
            for name in page.get("clusters", []):
                try:
                    desc = cli.describe_cluster(name=name).get("cluster", {})
                except Exception as exc:
                    desc = {}
                    _warn(warnings, "eks", region, "describe_cluster", exc)
                min_nodes = 0
                desired_nodes = 0
                max_nodes = 0
                try:
                    ngs = cli.list_nodegroups(clusterName=name).get("nodegroups", [])
                    for ng in ngs:
                        try:
                            ngd = cli.describe_nodegroup(clusterName=name, nodegroupName=ng).get("nodegroup", {})
                        except Exception as exc:
                            ngd = {}
                            _warn(warnings, "eks", region, "describe_nodegroup", exc)
                        sc = (ngd.get("scalingConfig") or {})
                        min_nodes += int(sc.get("minSize") or 0)
                        desired_nodes += int(sc.get("desiredSize") or 0)
                        max_nodes += int(sc.get("maxSize") or 0)
                        out_nodegroups.append({
                            "Region": region,
                            "ClusterName": name,
                            "NodeGroupName": ng,
                            "MinSize": sc.get("minSize"),
                            "DesiredSize": sc.get("desiredSize"),
                            "MaxSize": sc.get("maxSize")
                        })
                except Exception as exc:
                    _warn(warnings, "eks", region, "list_nodegroups", exc)
                out.append({
                    "Region": region,
                    "Name": name,
                    "Status": desc.get("status"),
                    "Version": desc.get("version"),
                    "Endpoint": desc.get("endpoint"),
                    "Arn": desc.get("arn"),
                    "NodeGroupsCount": len([n for n in out_nodegroups if n["ClusterName"] == name]),
                    "DesiredNodesTotal": desired_nodes,
                    "MinNodesTotal": min_nodes,
                    "MaxNodesTotal": max_nodes,
                    "Equivalente_GCP": "GKE (cluster gerenciado)"
                })
    except Exception as exc:
        _warn(warnings, "eks", region, "list_clusters", exc)
    return out, out_nodegroups

def collect_ecs(session, region, warnings=None):
    out_clusters, out_services, out_tasks = [], [], []
    try:
        cli = session.client("ecs", region_name=region)
        cluster_arns = []
        paginator = cli.get_paginator("list_clusters")
        for page in paginator.paginate():
            cluster_arns.extend(page.get("clusterArns", []))
        if cluster_arns:
            for i in range(0, len(cluster_arns), 100):
                chunk = cluster_arns[i:i+100]
                desc = cli.describe_clusters(clusters=chunk).get("clusters", [])
                for c in desc:
                    out_clusters.append({
                        "Region": region,
                        "ClusterArn": c.get("clusterArn"),
                        "ClusterName": c.get("clusterName"),
                        "Status": c.get("status"),
                        "RegisteredContainerInstancesCount": c.get("registeredContainerInstancesCount"),
                        "RunningTasksCount": c.get("runningTasksCount"),
                        "ActiveServicesCount": c.get("activeServicesCount"),
                    })
        for c in out_clusters:
            svc_arns = []
            paginator = cli.get_paginator("list_services")
            for page in paginator.paginate(cluster=c["ClusterArn"]):
                svc_arns.extend(page.get("serviceArns", []))
            desired_list = []
            running_list = []
            for i in range(0, len(svc_arns), 10):
                chunk = svc_arns[i:i+10]
                if not chunk:
                    continue
                ds = cli.describe_services(cluster=c["ClusterArn"], services=chunk)
                for s in ds.get("services", []):
                    out_services.append({
                        "Region": region,
                        "ClusterArn": c["ClusterArn"],
                        "ServiceArn": s.get("serviceArn"),
                        "ServiceName": s.get("serviceName"),
                        "Status": s.get("status"),
                        "DesiredCount": s.get("desiredCount"),
                        "RunningCount": s.get("runningCount"),
                        "LaunchType": s.get("launchType"),
                    })
                    desired_list.append(int(s.get("desiredCount") or 0))
                    running_list.append(int(s.get("runningCount") or 0))
            if desired_list:
                c["AvgServiceDesiredCount"] = round(sum(desired_list) / len(desired_list), 2)
            else:
                c["AvgServiceDesiredCount"] = 0
            if running_list:
                c["AvgServiceRunningCount"] = round(sum(running_list) / len(running_list), 2)
            else:
                c["AvgServiceRunningCount"] = 0
            c["Equivalente_GCP"] = "Cloud Run/GKE (containers)"
            try:
                task_arns = []
                paginator = cli.get_paginator("list_tasks")
                for page in paginator.paginate(cluster=c["ClusterArn"]):
                    task_arns.extend(page.get("taskArns", []))
                for j in range(0, len(task_arns), 100):
                    tchunk = task_arns[j:j+100]
                    if not tchunk:
                        continue
                    td = cli.describe_tasks(cluster=c["ClusterArn"], tasks=tchunk).get("tasks", [])
                    for t in td:
                        out_tasks.append({
                            "Region": region,
                            "ClusterArn": c["ClusterArn"],
                            "TaskArn": t.get("taskArn"),
                            "LastStatus": t.get("lastStatus"),
                            "LaunchType": t.get("launchType"),
                            "TaskDefinitionArn": t.get("taskDefinitionArn")
                        })
            except Exception as exc:
                _warn(warnings, "ecs", region, "list_or_describe_tasks", exc)
    except Exception as exc:
        _warn(warnings, "ecs", region, "list_or_describe_clusters_services", exc)
    return out_clusters, out_services, out_tasks

def collect_dynamodb(session, region, warnings=None):
    out = []
    try:
        cli = session.client("dynamodb", region_name=region)
        paginator = cli.get_paginator("list_tables")
        for page in paginator.paginate():
            for t in page.get("TableNames", []):
                try:
                    td = cli.describe_table(TableName=t).get("Table", {})
                except Exception as exc:
                    td = {}
                    _warn(warnings, "dynamodb", region, "describe_table", exc)
                size = td.get("TableSizeBytes")
                if size is not None:
                    eq = f"Firestore/Bigtable (~{size} bytes)"
                else:
                    eq = "Firestore/Bigtable"
                out.append({
                    "Region": region,
                    "TableName": t,
                    "Status": td.get("TableStatus"),
                    "BillingMode": (td.get("BillingModeSummary") or {}).get("BillingMode"),
                    "ItemCount": td.get("ItemCount"),
                    "SizeBytes": size,
                    "Equivalente_GCP": eq
                })
    except Exception as exc:
        _warn(warnings, "dynamodb", region, "list_tables", exc)
    return out

def collect_ecr(session, region, warnings=None):
    out = []
    try:
        cli = session.client("ecr", region_name=region)
        paginator = cli.get_paginator("describe_repositories")
        for page in paginator.paginate():
            for r in page.get("repositories", []):
                out.append({
                    "Region": region,
                    "RepositoryName": r.get("repositoryName"),
                    "RepositoryArn": r.get("repositoryArn"),
                    "Uri": r.get("repositoryUri"),
                    "ScanOnPush": (r.get("imageScanningConfiguration") or {}).get("scanOnPush"),
                    "ImmutableTags": r.get("imageTagMutability"),
                    "Equivalente_GCP": "Artifact Registry"
                })
    except Exception as exc:
        _warn(warnings, "ecr", region, "describe_repositories", exc)
    return out

def collect_region(session, region, warnings=None):
    bundle = {"Region": region}
    try:
        ec2 = collect_ec2(session, region, warnings=warnings)
        elb_v2, elb_classic = collect_elb(session, region, warnings=warnings)
        rds_instances, rds_clusters = collect_rds(session, region, warnings=warnings)
        lambdas = collect_lambda(session, region, warnings=warnings)
        eks, eks_nodegroups = collect_eks(session, region, warnings=warnings)
        ecs_clusters, ecs_services, ecs_tasks = collect_ecs(session, region, warnings=warnings)
        ddb = collect_dynamodb(session, region, warnings=warnings)
        ecr = collect_ecr(session, region, warnings=warnings)
        bundle.update({
            "EC2_Instances": ec2["Instances"],
            "EC2_Volumes": ec2["Volumes"],
            "EC2_Snapshots": ec2["Snapshots"],
            "EC2_SecurityGroups": ec2["SecurityGroups"],
            "EC2_VPCs": ec2["Vpcs"],
            "EC2_Subnets": ec2["Subnets"],
            "EC2_RouteTables": ec2["RouteTables"],
            "EC2_EIPs": ec2["Addresses"],
            "ELBv2_LoadBalancers": elb_v2,
            "ELB_Classic": elb_classic,
            "RDS_Instances": rds_instances,
            "RDS_Clusters": rds_clusters,
            "Lambda_Functions": lambdas,
            "EKS_Clusters": eks,
            "EKS_Nodegroups": eks_nodegroups,
            "ECS_Clusters": ecs_clusters,
            "ECS_Services": ecs_services,
            "ECS_Tasks": ecs_tasks,
            "DynamoDB_Tables": ddb,
            "ECR_Repositories": ecr,
        })
    except Exception as e:
        bundle["error"] = f"{type(e).__name__}: {e}"
        _warn(warnings, "regional_bundle", region, "collect_region", e)
    return bundle

SHEET_TO_COMPARECLOUD_AWS_NAMES = {
    "S3_Buckets": ["Amazon Simple Storage Service (S3)", "Amazon S3"],
    "CloudFront_Distributions": ["Amazon CloudFront", "CloudFront"],
    "Route53_Zones": ["Amazon Route 53", "Route 53"],
    "IAM_Users": ["AWS Identity and Access Management (IAM)", "Amazon IAM"],
    "IAM_Roles": ["AWS Identity and Access Management (IAM)", "Amazon IAM"],
    "IAM_Policies": ["AWS Identity and Access Management (IAM)", "Amazon IAM"],
    "ResourceExplorer2": ["AWS Resource Explorer"],
    "EC2_Instances": ["Amazon EC2"],
    "EC2_Volumes": ["Amazon Elastic Block Storage (EBS)", "Amazon EBS"],
    "EC2_Snapshots": ["Amazon Elastic Block Storage (EBS)", "Amazon EBS"],
    "EC2_SecurityGroups": ["Amazon Virtual Private Cloud (VPC)", "Amazon VPC"],
    "EC2_VPCs": ["Amazon Virtual Private Cloud (VPC)", "Amazon VPC"],
    "EC2_Subnets": ["Amazon Virtual Private Cloud (VPC)", "Amazon VPC"],
    "EC2_RouteTables": ["Amazon Virtual Private Cloud (VPC)", "Amazon VPC"],
    "EC2_EIPs": ["Amazon Elastic IP", "Elastic IP Address"],
    "ELBv2_LoadBalancers": ["Elastic Load Balancing", "Application Load Balancer", "Network Load Balancer"],
    "ELB_Classic": ["Elastic Load Balancing", "Classic Load Balancer"],
    "RDS_Instances": ["Amazon Relational Database Service (RDS)", "Amazon RDS"],
    "RDS_Clusters": ["Amazon Relational Database Service (RDS)", "Amazon Aurora", "Amazon DocumentDB"],
    "Lambda_Functions": ["AWS Lambda"],
    "EKS_Clusters": ["Amazon Elastic Kubernetes Service (EKS)", "Amazon EKS"],
    "EKS_Nodegroups": ["Amazon Elastic Kubernetes Service (EKS)", "Amazon EKS"],
    "ECS_Clusters": ["Amazon Elastic Container Service (ECS)", "Amazon ECS"],
    "ECS_Services": ["Amazon Elastic Container Service (ECS)", "Amazon ECS"],
    "ECS_Tasks": ["Amazon Elastic Container Service (ECS)", "Amazon ECS"],
    "DynamoDB_Tables": ["Amazon DynamoDB"],
    "ECR_Repositories": ["Amazon Elastic Container Registry (ECR)", "Amazon ECR"],
}


def _rds_sheet_candidates(row):
    engine = str((row or {}).get("Engine") or "").lower()
    if "docdb" in engine:
        return ["Amazon DocumentDB", "Amazon RDS"]
    if "aurora" in engine:
        return ["Amazon Aurora", "Amazon RDS"]
    return ["Amazon Relational Database Service (RDS)", "Amazon RDS"]


def _sheet_candidates(sheet_name, row):
    if sheet_name in ("RDS_Instances", "RDS_Clusters"):
        return _rds_sheet_candidates(row)
    return SHEET_TO_COMPARECLOUD_AWS_NAMES.get(sheet_name, [])


def _apply_comparecloud_equivalents(sheets, mapper):
    if not mapper:
        return sheets
    for sheet_name, rows in (sheets or {}).items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row.pop("Equivalente_GCP", None)
            candidates = _sheet_candidates(sheet_name, row)
            if not candidates:
                continue
            row["Equivalent_GCP"] = mapper.format_equivalents("aws", candidates, "google")
            row["Equivalent_Azure"] = mapper.format_equivalents("aws", candidates, "azure")
            row["Equivalence_Source"] = mapper.source_url
    return sheets


def _merge_region_bundles(region_bundles, key):
    out = []
    for bundle in region_bundles:
        out.extend(bundle.get(key, []))
    return out


def build_aws_sheets(session, home_region="us-east-1", threads=8, mapper=None):
    warnings = []
    sts = session.client("sts")
    account_id = sts.get_caller_identity()["Account"]

    s3 = collect_s3(session, warnings=warnings)
    cf = collect_cloudfront(session, warnings=warnings)
    r53 = collect_route53(session, warnings=warnings)
    iam_users, iam_roles, iam_policies = collect_iam(session, warnings=warnings)
    rex_items, rex_status = collect_resource_explorer(session, home_region)
    if rex_status != "ok":
        warnings.append(
            {
                "Provider": "aws",
                "Service": "resource-explorer-2",
                "Region": home_region,
                "Operation": "search",
                "ErrorType": "Unavailable",
                "ErrorMessage": f"Resource Explorer status: {rex_status}",
                "CapturedAt": dt_to_br_str(datetime.now(timezone.utc)),
            }
        )

    regions = [r for r in available_regions(session) if is_region_reachable(session, r)]
    region_bundles = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = {ex.submit(collect_region, session, region, warnings): region for region in regions}
        for future in as_completed(futures):
            region_bundles.append(future.result())

    sheets = {}
    collected_at = dt_to_br_str(datetime.now(timezone.utc))
    sheets["META"] = [{
        "Provider": "aws",
        "AccountId": account_id,
        "CollectedAt": collected_at,
        "Regions_Count": len(regions),
        "ResourceExplorer2_Status": rex_status,
        "ResourceExplorer2_Count": len(rex_items),
        "Warnings_Count": len(warnings),
    }]
    sheets["S3_Buckets"] = s3
    sheets["CloudFront_Distributions"] = cf
    sheets["Route53_Zones"] = r53
    sheets["IAM_Users"] = iam_users
    sheets["IAM_Roles"] = iam_roles
    sheets["IAM_Policies"] = iam_policies
    if rex_items:
        sheets["ResourceExplorer2"] = rex_items
    if warnings:
        sheets["WARNINGS"] = warnings

    sheets["EC2_Instances"] = _merge_region_bundles(region_bundles, "EC2_Instances")
    sheets["EC2_Volumes"] = _merge_region_bundles(region_bundles, "EC2_Volumes")
    sheets["EC2_Snapshots"] = _merge_region_bundles(region_bundles, "EC2_Snapshots")
    sheets["EC2_SecurityGroups"] = _merge_region_bundles(region_bundles, "EC2_SecurityGroups")
    sheets["EC2_VPCs"] = _merge_region_bundles(region_bundles, "EC2_VPCs")
    sheets["EC2_Subnets"] = _merge_region_bundles(region_bundles, "EC2_Subnets")
    sheets["EC2_RouteTables"] = _merge_region_bundles(region_bundles, "EC2_RouteTables")
    sheets["EC2_EIPs"] = _merge_region_bundles(region_bundles, "EC2_EIPs")
    sheets["ELBv2_LoadBalancers"] = _merge_region_bundles(region_bundles, "ELBv2_LoadBalancers")
    sheets["ELB_Classic"] = _merge_region_bundles(region_bundles, "ELB_Classic")
    sheets["RDS_Instances"] = _merge_region_bundles(region_bundles, "RDS_Instances")
    sheets["RDS_Clusters"] = _merge_region_bundles(region_bundles, "RDS_Clusters")
    sheets["Lambda_Functions"] = _merge_region_bundles(region_bundles, "Lambda_Functions")
    sheets["EKS_Clusters"] = _merge_region_bundles(region_bundles, "EKS_Clusters")
    sheets["EKS_Nodegroups"] = _merge_region_bundles(region_bundles, "EKS_Nodegroups")
    sheets["ECS_Clusters"] = _merge_region_bundles(region_bundles, "ECS_Clusters")
    sheets["ECS_Services"] = _merge_region_bundles(region_bundles, "ECS_Services")
    sheets["ECS_Tasks"] = _merge_region_bundles(region_bundles, "ECS_Tasks")
    sheets["DynamoDB_Tables"] = _merge_region_bundles(region_bundles, "DynamoDB_Tables")
    sheets["ECR_Repositories"] = _merge_region_bundles(region_bundles, "ECR_Repositories")

    return _apply_comparecloud_equivalents(sheets, mapper)


def collect_aws_inventory(
    access_key_id,
    secret_access_key,
    session_token=None,
    default_region="us-east-1",
    home_region="us-east-1",
    threads=8,
    mapper=None,
):
    session = make_session(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        default_region=default_region,
    )
    return build_aws_sheets(session=session, home_region=home_region, threads=threads, mapper=mapper)


def sheets_to_workbook_bytes(sheets):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
        for name, rows in (sheets or {}).items():
            write_sheet(writer, name, rows)
    buffer.seek(0)
    return buffer.read()
