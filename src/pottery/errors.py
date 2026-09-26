"""领域错误类型，映射为对应的 HTTP 状态码。"""


class DomainError(Exception):
    http_status = 400
    code = "domain_error"


class NotFound(DomainError):
    http_status = 404
    code = "not_found"


class Conflict(DomainError):
    http_status = 409
    code = "conflict"


class ValidationFailed(DomainError):
    http_status = 422
    code = "validation_failed"


class Unauthorized(DomainError):
    http_status = 401
    code = "unauthorized"


class Forbidden(DomainError):
    http_status = 403
    code = "forbidden"


class StepOutOfOrder(Conflict):
    code = "step_out_of_order"


class PrerequisiteNotMet(Conflict):
    code = "prerequisite_not_met"


class GlazeIncompatible(Conflict):
    code = "glaze_incompatible"


class MaterialForbidden(Conflict):
    code = "material_forbidden"


class BatchFull(Conflict):
    code = "batch_full"


class BatchNotEditable(Conflict):
    code = "batch_not_editable"


class DuplicateUpload(Conflict):
    code = "duplicate_upload"


class WorkNotEligible(Conflict):
    code = "work_not_eligible"


class InvalidDisposition(Conflict):
    code = "invalid_disposition"
