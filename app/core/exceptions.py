class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

class PlatformNotSupportedError(AppError):
    status_code = 404
    code = "platform_not_supported"

class RequestValidationError(AppError):
    status_code = 422
    code = "invalid_request"

class JobNotFoundError(AppError):
    status_code = 404
    code = "job_not_found"

class ScrapingError(AppError):
    status_code = 502
    code = "scraping_failed"

class PlatformNotImplementedError(AppError):
    status_code = 501
    code = "platform_not_implemented"

class ReportNotFoundError(AppError):
    status_code = 404
    code = "report_not_found"

class SummaryNotFoundError(AppError):
    status_code = 404
    code = "summary_not_found"

class ArtifactNotFoundError(AppError):
    status_code = 404
    code = "artifact_not_found"

class NoDataError(AppError):
    status_code = 422
    code = "no_data_for_filters"

class ReportGenerationError(AppError):
    status_code = 500
    code = "report_generation_failed"
