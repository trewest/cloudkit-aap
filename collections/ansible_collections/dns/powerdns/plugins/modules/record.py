#!/usr/bin/python
# -*- coding: utf-8 -*-

# GNU General Public License v3.0+
# (see https://www.gnu.org/licenses/gpl-3.0.txt)

DOCUMENTATION = r"""
---
module: record
short_description: Manage DNS records in PowerDNS via its REST API
description:
  - Create, update, or delete DNS records in a PowerDNS Authoritative Server
    using the HTTP API.
  - Supports A and AAAA record types.
  - Idempotent — checks current record state before making changes.
version_added: "1.0.0"
options:
  state:
    description: Whether the record should exist or not.
    type: str
    choices: [present, absent]
    default: present
  zone:
    description:
      - The DNS zone (domain) to operate in.
      - Will be dot-terminated automatically if needed.
    type: str
    required: true
  record:
    description:
      - Fully qualified domain name for the record.
      - Will be dot-terminated automatically if needed.
    type: str
    required: true
  type:
    description: DNS record type.
    type: str
    choices: [A, AAAA]
    default: A
  value:
    description:
      - The value for the record (IPv4 for A, IPv6 for AAAA).
      - Required when I(state=present).
    type: str
  ttl:
    description: TTL in seconds.
    type: int
    default: 1800
  overwrite:
    description:
      - Whether to overwrite an existing record.
      - When C(true), uses REPLACE changetype.
      - When C(false), uses CREATE changetype (fails if record exists).
    type: bool
    default: true
  server_url:
    description: URL of the PowerDNS API (e.g., C(http://pdns.example.com:8081)).
    type: str
    required: true
  server_id:
    description: PowerDNS server ID. Almost always C(localhost) for the authoritative server.
    type: str
    default: localhost
  api_key:
    description: PowerDNS API key for authentication.
    type: str
    required: true
    no_log: true
author:
  - OSAC Project
"""

EXAMPLES = r"""
- name: Create an A record
  dns.powerdns.record:
    state: present
    zone: example.com
    record: app.example.com
    type: A
    value: 192.168.1.10
    ttl: 300
    server_url: http://pdns.example.com:8081
    api_key: "{{ powerdns_api_key }}"

- name: Delete an A record
  dns.powerdns.record:
    state: absent
    zone: example.com
    record: app.example.com
    type: A
    server_url: http://pdns.example.com:8081
    api_key: "{{ powerdns_api_key }}"
"""

RETURN = r"""
zone:
  description: The zone that was modified.
  returned: always
  type: str
record:
  description: The record name that was modified.
  returned: always
  type: str
type:
  description: The record type.
  returned: always
  type: str
"""

import json
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url


def ensure_trailing_dot(name):
    if name and not name.endswith("."):
        return name + "."
    return name


def build_api_url(server_url, server_id, zone):
    return "{0}/api/v1/servers/{1}/zones/{2}".format(
        server_url.rstrip("/"), server_id, zone
    )


def get_existing_records(module, url, headers, record_name, record_type):
    resp, info = fetch_url(module, url, headers=headers, method="GET")

    if info["status"] == 404:
        return None

    if info["status"] != 200:
        module.fail_json(
            msg="Failed to query zone: HTTP {0}".format(info["status"]),
            response=info.get("body", ""),
        )

    try:
        zone_data = json.loads(resp.read())
    except (ValueError, AttributeError) as e:
        module.fail_json(msg="Failed to parse zone response: {0}".format(str(e)))

    for rrset in zone_data.get("rrsets", []):
        if rrset["name"] == record_name and rrset["type"] == record_type:
            return rrset

    return None


def record_matches(existing_rrset, value, ttl):
    if existing_rrset is None:
        return False

    if existing_rrset.get("ttl") != ttl:
        return False

    existing_values = {
        r["content"] for r in existing_rrset.get("records", []) if not r.get("disabled")
    }
    return existing_values == {value}


def patch_zone(module, url, headers, body):
    resp, info = fetch_url(
        module,
        url,
        headers=headers,
        method="PATCH",
        data=json.dumps(body),
    )

    if info["status"] != 204:
        error_body = ""
        if resp:
            try:
                error_body = resp.read()
            except Exception:
                pass
        elif info.get("body"):
            error_body = info["body"]

        module.fail_json(
            msg="PowerDNS API error: HTTP {0}".format(info["status"]),
            response=error_body,
        )


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            state=dict(type="str", default="present", choices=["present", "absent"]),
            zone=dict(type="str", required=True),
            record=dict(type="str", required=True),
            type=dict(type="str", default="A", choices=["A", "AAAA"]),
            value=dict(type="str"),
            ttl=dict(type="int", default=1800),
            overwrite=dict(type="bool", default=True),
            server_url=dict(type="str", required=True),
            server_id=dict(type="str", default="localhost"),
            api_key=dict(type="str", required=True, no_log=True),
        ),
        required_if=[
            ("state", "present", ["value"]),
        ],
        supports_check_mode=True,
    )

    state = module.params["state"]
    zone = ensure_trailing_dot(module.params["zone"])
    record_name = ensure_trailing_dot(module.params["record"])
    record_type = module.params["type"]
    value = module.params.get("value")
    ttl = module.params["ttl"]
    overwrite = module.params["overwrite"]
    server_url = module.params["server_url"]
    server_id = module.params["server_id"]
    api_key = module.params["api_key"]

    url = build_api_url(server_url, server_id, zone)
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    result = dict(
        changed=False,
        zone=zone,
        record=record_name,
        type=record_type,
    )

    existing = get_existing_records(module, url, headers, record_name, record_type)

    if state == "present":
        if record_matches(existing, value, ttl):
            module.exit_json(**result)

        result["changed"] = True

        if module.check_mode:
            module.exit_json(**result)

        body = {
            "rrsets": [
                {
                    "name": record_name,
                    "type": record_type,
                    "ttl": ttl,
                    "changetype": "REPLACE" if overwrite else "CREATE",
                    "records": [{"content": value, "disabled": False}],
                }
            ]
        }
        patch_zone(module, url, headers, body)

    elif state == "absent":
        if existing is None:
            module.exit_json(**result)

        result["changed"] = True

        if module.check_mode:
            module.exit_json(**result)

        body = {
            "rrsets": [
                {
                    "name": record_name,
                    "type": record_type,
                    "changetype": "DELETE",
                }
            ]
        }
        patch_zone(module, url, headers, body)

    module.exit_json(**result)


def main():
    run_module()


if __name__ == "__main__":
    main()
