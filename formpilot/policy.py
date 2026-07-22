from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field


SECRET_PATTERN = re.compile(r"验证码|captcha|短信码|动态码|密码|password|口令|签名", re.I)
SIDE_EFFECT_PATTERN = re.compile(r"提交|注册|登录|保存|发送|获取验证码|同意|确认|支付|删除|上传", re.I)
SAFE_NAVIGATION_PATTERN = re.compile(r"^(下一步|上一步|返回|继续|下一页|上一页)$")


@dataclass(slots=True)
class ApprovalPolicy:
    approvals: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def secret_field(field: dict) -> bool:
        text = " ".join(str(field.get(key, "")) for key in ("label", "name", "id", "placeholder", "type"))
        return bool(SECRET_PATTERN.search(text)) or field.get("type") in {"password", "file"}

    @staticmethod
    def click_requires_confirmation(control: dict) -> bool:
        label = str(control.get("label", "")).strip()
        if SIDE_EFFECT_PATTERN.search(label):
            return True
        return not bool(SAFE_NAVIGATION_PATTERN.fullmatch(label))

    def issue(self, target: str) -> str:
        approval_id = secrets.token_urlsafe(18)
        self.approvals[approval_id] = target
        return approval_id

    def consume(self, approval_id: str | None, target: str) -> bool:
        if not approval_id or self.approvals.get(approval_id) != target:
            return False
        del self.approvals[approval_id]
        return True
