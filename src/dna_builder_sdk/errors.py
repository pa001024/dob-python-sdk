"""统一异常模型：HTTP 层与 GraphQL 层共用，调用方可按 code 分支。"""


class DobApiError(Exception):
    """SDK 异常基类，携带机器可读的 code。"""

    def __init__(self, message: str, code: str = "unknown", payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class DobHttpError(DobApiError):
    """HTTP 状态异常（含 302 跟踪失败、超时、连接失败）。"""

    def __init__(self, message: str, status: int = 0, code: str = "http_error", payload=None):
        super().__init__(message, code, payload)
        self.status = status


class DobGraphQLError(DobApiError):
    """GraphQL errors 数组非空时抛出，message 为首条错误信息拼接。"""

    def __init__(self, message: str, errors=None):
        super().__init__(message, code="graphql_error", payload=errors)
        self.errors = errors or []
