# OSAC Carbide Integration

## Overview

The OSAC Carbide integration enables cluster-as-a-service provisioning on bare metal infrastructure managed by NVIDIA Carbide Bare Metal Manager (BMM). When a ClusterOrder is submitted with the `ocp_4_20_small_carbide` template, the fulfillment system automatically provisions OpenShift 4.20 clusters with per-cluster network isolation using Carbide VPCs. This integration uses the Ansible Automation Platform (AAP) with the nvidia.bare_metal collection to manage infrastructure lifecycle operations, including VPC creation, VPC peering, compute instance provisioning, agent discovery and labeling, and MetalLB ingress configuration with BGP routing.

## Prerequisites

1. **Carbide API Access**
   - Active Carbide instance with accessible API endpoint
   - OAuth2 client credentials (client ID and secret) for authentication
   - Organization, site, and tenant identifiers configured in Carbide
   - Valid IP block with available CIDR ranges for VPC prefixes

2. **AAP Environment**
   - Ansible Automation Platform with osac-aap collection installed
   - `cluster-fulfillment-ig` Kubernetes Secret containing all `NVIDIA_BMM_*` environment variables
   - `oc` CLI tool available in the execution environment (for BGP discovery)

3. **Kubernetes Infrastructure**
   - Management cluster running the OSAC operator and Hosted Control Planes (HCP) operator
   - `hardware-inventory` namespace for agent management and state tracking
   - InfraEnv resource configured for bare metal agent discovery

4. **Network Infrastructure**
   - Management cluster VPC ID for VPC peering
   - Ingress CIDR range for MetalLB IP allocation
   - BGP connectivity between bare metal nodes and network fabric

## Environment Variables

All Carbide configuration is provided via environment variables injected into AAP from the `cluster-fulfillment-ig` Kubernetes Secret.

| Variable | Required | Description |
|----------|----------|-------------|
| `NVIDIA_BMM_API_URL` | Yes | Carbide API endpoint (e.g., `https://carbide.example.com`) |
| `NVIDIA_BMM_CLIENT_ID` | Yes | OAuth2 client ID |
| `NVIDIA_BMM_CLIENT_SECRET` | Yes | OAuth2 client secret |
| `NVIDIA_BMM_SSA_TOKEN_URL` | Yes | OAuth2 token exchange endpoint |
| `NVIDIA_BMM_ORG` | Yes | Organization identifier |
| `NVIDIA_BMM_SITE_ID` | Yes | Site identifier |
| `NVIDIA_BMM_TENANT_ID` | Yes | Tenant identifier |
| `NVIDIA_BMM_MGMT_VPC_ID` | Yes | Management cluster VPC ID for VPC peering |
| `NVIDIA_BMM_INGRESS_CIDR` | Yes | CIDR range for ingress IP allocation (e.g., `10.0.100.0/24`) |
| `NVIDIA_BMM_DEFAULT_IP_BLOCK_ID` | No | Default IP block for VPC prefix allocation |
| `NVIDIA_BMM_SSH_KEY_GROUP_ID` | No | Default SSH key group for instance access |
| `NVIDIA_BMM_DEFAULT_OS_ID` | No | Default operating system ID for instances |
| `NVIDIA_BMM_API_PATH_PREFIX` | No | API path prefix (default: `forge`) |
| `NVIDIA_BMM_OAUTH_SCOPE` | No | OAuth2 scope (default: empty) |
| `NVIDIA_BMM_IPXE_DNS_SERVER` | No | DNS server to inject into iPXE boot script |

## Template Parameters

ClusterOrder resources using `ocp_4_20_small_carbide` provide these in `templateParameters`:

| Parameter | Required | Type | Description |
|-----------|----------|------|-------------|
| `pull_secret` | Yes | string | OpenShift pull secret for image registry authentication |
| `ssh_public_key` | Yes | string | SSH public key for cluster node access |
| `ip_block_id` | Yes | string | Carbide IP block ID for VPC prefix allocation |
| `local_asn` | Yes | integer | Local ASN for MetalLB BGP speaker (shared by all workers) |
| `ocp_release_image` | No | string | Override OCP release image (default: 4.20.0-multi) |
| `vpc_id` | No | string | Use existing VPC instead of creating new |
| `vpc_prefix_id` | No | string | Use existing VPC prefix instead of creating new |
| `ssh_key_group_id` | No | string | Override SSH key group |

## Cluster Creation Flow

When a ClusterOrder with the Carbide template is processed:

### 1. OAuth2 Authentication
The `carbide_infra` role exchanges client credentials for a JWT token via the SSA token endpoint. All subsequent Carbide API calls use this token.

### 2. VPC and Network Setup
- **VPC**: Creates `osac-vpc-{cluster_name}` (or uses existing if `vpc_id` provided)
- **VPC Prefix**: Creates `prefix-{cluster_name}` in the specified IP block (or uses existing if `vpc_prefix_id` provided)
- **VPC Peering**: Peers the cluster VPC with the management cluster VPC so nodes can reach the control plane

### 3. Instance Provisioning
For each resource class in the node requests:
- Looks up the Carbide instance type by name (resource class name must match instance type name)
- Creates instances in the cluster VPC with iPXE boot from InfraEnv
- Optionally injects DNS server into iPXE script
- Supports batch provisioning when topology optimization is enabled

### 4. Agent Discovery and Labeling
- Waits for instances to boot and register as Kubernetes Agents via MAC address correlation
- Labels agents with `osac.io/cluster`, `osac.io/resource-class`, and `nvidia-bmm-instance-id`
- Agents are then selected and approved via the `manage_agents` workflow

### 5. HostedCluster and NodePool Creation
- Creates HyperShift HostedCluster with the configured OCP release image
- Creates NodePools matching the requested resource classes
- Agents are attached to the cluster via label selectors

### 6. External Access (MetalLB + BGP)
- Allocates a `/32` ingress IP from the configured CIDR range, tracked in a `carbide-ip-registry` ConfigMap
- Creates DNS records (api, api-int, *.apps) if Route53 is configured
- Installs MetalLB operator and creates IPAddressPool with the allocated IP
- Discovers BGP parameters from worker nodes:
  - **Gateway**: extracted from `k8s.ovn.org/l3-gateway-config` node annotation
  - **Peer ASN**: discovered via `oc debug node` + curl to Carbide metadata service (`169.254.169.254:7777`)
- Creates BGPAdvertisement and per-worker BGPPeer resources

### 7. State Persistence
All infrastructure state (VPC ID, prefix ID, instance IDs, peering ID, creation flags) is stored in a `carbide-infra-{cluster_name}` ConfigMap in the `hardware-inventory` namespace.

## Cluster Deletion Flow

Cleanup occurs in reverse order:

1. **Hosted Cluster Deletion** - Remove HyperShift HostedCluster
2. **External Access Cleanup** - Remove ingress IP from registry, delete DNS records
3. **Agent Detachment** - Unlabel and detach agents from cluster
4. **Instance Deletion** - Destroy all Carbide instances by ID from ConfigMap
5. **VPC Peering Deletion** - Remove peering with management cluster
6. **VPC Prefix Deletion** - Delete prefix (only if created by this cluster)
7. **VPC Deletion** - Delete VPC (only if created by this cluster)
8. **State Cleanup** - Delete infrastructure ConfigMap

Resources that were pre-existing (passed via `vpc_id` or `vpc_prefix_id`) are not deleted.

## Architecture

### Backend Routing

The template sets `infrastructure_provider: nvidia_bmm` as a fact at the start of install/delete. Shared roles use this to dispatch to provider-specific logic:

- **`cluster_infra`**: Routes to `carbide_infra` (Carbide) or `massopencloud.esi.network` (ESI)
- **`external_access`**: Routes to Carbide IP allocation or ESI floating IPs
- **`metallb_ingress`**: Creates BGPAdvertisement (Carbide) or L2Advertisement (ESI)
- **`manage_agents`**: Skips ESI L2 network switching for Carbide (agents already in cluster VPC)

### Agent Resource Class Label

Carbide agents use `osac.io/resource-class` instead of the ESI-specific `esi.nerc.mghpcc.org/resource_class`. Shared roles use `agent_resource_class_label | default(esi_agent_resource_class_label)` so ESI continues working without changes.

### State Storage

| ConfigMap | Namespace | Purpose |
|-----------|-----------|---------|
| `carbide-infra-{cluster}` | `hardware-inventory` | VPC, prefix, instance, and peering IDs + creation flags |
| `carbide-ip-registry` | `hardware-inventory` | Ingress IP allocations (IP → cluster name mapping) |

## Troubleshooting

### Instance Type Not Found
```
Carbide instance type 'X' not found at site 'Y'
```
The resource class name in `nodeRequests` must exactly match a Carbide instance type name. Check available types in the Carbide UI or API.

### SSH Key Group Error
```
Invalid SSH Key Group ID: specified in request
```
Set `NVIDIA_BMM_SSH_KEY_GROUP_ID` environment variable or provide `ssh_key_group_id` in template parameters.

### Agent Registration Timeout
Agents not registering after instance boot:
- Check InfraEnv exists: `kubectl get infraenv -n hardware-inventory`
- Verify iPXE script URL is accessible
- Check DNS resolution if `NVIDIA_BMM_IPXE_DNS_SERVER` is set
- Review instance status in Carbide UI

### BGP Discovery Failure
Gateway or ASN discovery failed:
- Gateway is extracted from node annotation `k8s.ovn.org/l3-gateway-config` — verify it exists
- ASN is discovered via `oc debug node` — verify `oc` is available and kubeconfig is valid
- Test manually: `oc debug node/<name> -- chroot /host curl -s http://169.254.169.254:7777/latest/meta-data/asn`

### IP Registry Not Cleaned Up
After cluster deletion, IP still in registry:
- Verify `external_access` step runs in delete template
- Check `carbide-ip-registry` ConfigMap: `kubectl get cm -n hardware-inventory carbide-ip-registry -o yaml`
- Manual cleanup: edit ConfigMap to remove the entry

### VPC/Instances Not Deleted
Infrastructure not cleaned up on delete:
- Check `carbide-infra-{cluster}` ConfigMap exists in `hardware-inventory` (not the cluster working namespace)
- Verify ConfigMap has valid instance IDs and VPC IDs
- Check Carbide API credentials are still valid during deletion

### Debugging Commands

```bash
# View infrastructure state
kubectl get cm -n hardware-inventory carbide-infra-{cluster} -o yaml

# Check IP allocations
kubectl get cm -n hardware-inventory carbide-ip-registry -o yaml

# List agents for a cluster
kubectl get agents -n hardware-inventory -l osac.io/cluster={cluster}

# Check MetalLB in hosted cluster
kubectl --kubeconfig={kubeconfig} get ipaddresspool,bgpadvertisement,bgppeer -n metallb-system

# Check ingress service
kubectl --kubeconfig={kubeconfig} get svc -n openshift-ingress external-ingress
```
