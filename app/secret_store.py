"""
Kho bí mật thay đổi lúc chạy (token Zalo, phiên Phương án 2), lưu trên đĩa ở dạng mã hóa AES-256-GCM.
Mỗi mục một file riêng: gateway (ghi phiên web) và worker (xoay token Zalo) không ghi đè lên nhau.
Khóa giải mã (DATA_KEY) đến từ biến môi trường / Docker secret, không bao giờ nằm cạnh file dữ liệu.
Khi chuyển sang HashiCorp Vault/Infisical chỉ cần thay lớp này, giữ nguyên giao diện get/put.
"""

import json
import os
import re
import tempfile

from .security import Sealer

_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class SecretStore:
    def __init__(self, directory: str, data_key_hex: str):
        self.directory = directory
        self._sealer = Sealer(data_key_hex)
        os.makedirs(directory, exist_ok=True)

    def path_for(self, name: str) -> str:
        if not _NAME_RE.match(name):
            raise ValueError("Tên mục bí mật không hợp lệ")
        return os.path.join(self.directory, f"{name}.enc")

    def get(self, name: str):
        path = self.path_for(name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            sealed = f.read().strip()
        return json.loads(self._sealer.open(sealed, purpose=name)) if sealed else None

    def put(self, name: str, value) -> None:
        sealed = self._sealer.seal(json.dumps(value).encode("utf-8"), purpose=name)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(sealed)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path_for(name))
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if os.path.exists(path):
            os.remove(path)
