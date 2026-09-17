from __future__ import annotations

import re
import secrets
from typing import Any

from harness.core import HarnessError

SECRET_KEYS = re.compile(r"(authorization|api.?key|access.?token|refresh.?token|password|cookie|secret|ticket)",re.I)


class CredentialVault:
    """OS-backed vault; plain credential values are never stored in business config."""
    def __init__(self, service: str = "local-agent-harness"):
        self.service = service
        self._redactions: set[str] = set()

    def put(self, target: str, value: str) -> str:
        import keyring
        if not target or not value or len(value) > 32768:
            raise HarnessError("INVALID_CREDENTIAL", "凭据或绑定目标无效",422)
        identity = secrets.token_hex(16)
        reference = f"credential:{target}:{identity}"
        keyring.set_password(self.service,reference,value)
        self._redactions.add(value)
        return reference

    def get(self, reference: str, target: str) -> str:
        import keyring
        if not reference.startswith(f"credential:{target}:"):
            raise HarnessError("CREDENTIAL_SCOPE", "凭据不能用于另一服务",403)
        result = keyring.get_password(self.service,reference)
        if result is None:
            raise HarnessError("CREDENTIAL_MISSING", "凭据已删除或不可用",422)
        self._redactions.add(result)
        return result

    def delete(self, reference: str, target: str):
        import keyring
        self.get(reference,target)
        keyring.delete_password(self.service,reference)

    def redact(self, value: Any) -> Any:
        if isinstance(value,dict):
            return {k:("[redacted]" if SECRET_KEYS.search(k) and not k.endswith("_ref") else self.redact(v)) for k,v in value.items()}
        if isinstance(value,list):
            return [self.redact(v) for v in value]
        if isinstance(value,str):
            for secret in self._redactions:
                value = value.replace(secret,"[redacted]")
            return value
        return value


def assert_secret_refs(config: Any):
    if isinstance(config,dict):
        for key,value in config.items():
            if key == "environment_refs":
                if not isinstance(value,dict) or any(not isinstance(ref,str) or not ref.startswith("credential:") for ref in value.values()):
                    raise HarnessError("SECRET_IN_CONFIG", "进程环境只接受绑定到服务的 credential: 引用",422)
                continue  # Environment names such as API_KEY are labels, never secret values.
            if key == "credential_ref" and value is not None and (not isinstance(value,str) or not value.startswith("credential:")):
                raise HarnessError("SECRET_IN_CONFIG", "凭据字段必须使用 credential: 引用",422)
            if SECRET_KEYS.search(key) and not key.endswith("_ref") and not key.endswith("_refs") and value:
                raise HarnessError("SECRET_IN_CONFIG", "普通配置仅接受凭据引用",422)
            assert_secret_refs(value)
    elif isinstance(config,list):
        for value in config:
            assert_secret_refs(value)
