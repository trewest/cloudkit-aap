import ipaddress


def cidr_hosts(value):
    """Returns a list of all host IP addresses in a CIDR range.

    Example:
        "10.0.100.0/28" | osac.service.cidr_hosts
        => ["10.0.100.1", "10.0.100.2", ..., "10.0.100.14"]

    Network and broadcast addresses are excluded.
    """
    network = ipaddress.ip_network(value, strict=False)
    return [str(ip) for ip in network.hosts()]


class FilterModule:
    def filters(self):
        return {
            "cidr_hosts": cidr_hosts,
        }
