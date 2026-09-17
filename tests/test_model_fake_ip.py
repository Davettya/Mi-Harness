"""Official HTTPS model grants may traverse TUN Fake-IP, without general SSRF bypass."""

import socket

import pytest

from harness.core import HarnessError
from harness.model_gateway.presets import PRESETS
from harness.platform.config import Settings
from harness.policy.engine import ModelSetupGrant
from harness.server.composition import Services


@pytest.fixture
def setup(tmp_path, monkeypatch):
    services = Services(Settings(data_dir=tmp_path, permissions={"network": True}))
    ctx = services.diagnostic("local", "fake-ip-regression")

    def resolve(addresses):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
            (2, 1, 6, "", (address, 443)) for address in addresses
        ])

    resolve(["198.18.0.88"])

    def check(url="https://api.deepseek.com", purpose="model", grant=True, saved=False):
        authority = ModelSetupGrant("local", ctx.diagnostic_id, "model@1", url)
        if saved:
            services.store.put("model_endpoint_grants", "model@1", {
                "owner_id": "local", "profile_ref": "model@1", "endpoint": url, "enabled": True,
            })
        return services.policy.endpoint(
            url, ctx, purpose=purpose, configured=True, model_profile_ref="model@1",
            model_setup_grant=authority if grant and not saved else None,
        )

    return services, resolve, check


@pytest.mark.parametrize("provider", ["openai", "anthropic", "deepseek", "qwen", "tencent", "siliconflow"])
def test_official_https_fake_ip_diagnostic_and_saved_grants(setup, provider):
    _, _, check = setup
    url = PRESETS[provider]["default_base_url"]
    assert check(url) == ["198.18.0.88"]
    assert check(url, saved=True) == ["198.18.0.88"]


@pytest.mark.parametrize("url,purpose,grant", [
    ("http://api.deepseek.com", "model", True),
    ("https://api.deepseek.com:8443", "model", True),
    ("https://api.deepseek.com/other", "model", True),
    ("https://api.deepseek.com?target=other", "model", True),
    ("https://custom.example/v1", "model", True),
    ("https://198.18.0.88", "model", True),
    ("https://api.deepseek.com", "fetch", True),
    ("https://api.deepseek.com", "mcp", True),
    ("https://api.deepseek.com", "model", False),
])
def test_fake_ip_exception_requires_exact_official_model_authority(setup, url, purpose, grant):
    _, _, check = setup
    with pytest.raises(HarnessError) as error:
        check(url, purpose, grant)
    assert error.value.code == "SSRF_DENIED"


@pytest.mark.parametrize("address", ["10.0.0.1", "169.254.169.254", "192.0.2.1", "fc00::1", "127.0.0.1"])
def test_fake_ip_mixed_with_private_or_other_reserved_addresses_is_denied(setup, address):
    _, resolve, check = setup
    resolve(["198.18.0.88", address])
    with pytest.raises(HarnessError) as error:
        check()
    assert error.value.code == "SSRF_DENIED"


@pytest.mark.parametrize("policy", [
    {"egress": "local_only"},
    {"egress": "domain_allowlist", "domains": ["other.example"]},
])
def test_fake_ip_does_not_bypass_egress_restrictions(setup, policy):
    services, _, check = setup
    services.store.put("config/policies", "default", policy)
    with pytest.raises(HarnessError) as error:
        check()
    assert error.value.code == "EGRESS_DENIED"


def test_fake_ip_does_not_bypass_platform_hosts_or_revocation(setup):
    services, _, check = setup
    assert check(saved=True)
    services.store.put("model_endpoint_grants", "model@1", {"enabled": False})
    with pytest.raises(HarnessError):
        check(grant=False)
    services.policy.set_environment_cap(services.settings.permissions.model_copy(
        update={"allowed_hosts": ["other.example"]}
    ))
    with pytest.raises(HarnessError) as error:
        check()
    assert error.value.code == "EGRESS_DENIED"
