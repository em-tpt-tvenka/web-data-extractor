class AppError(Exception):
    """Base class for domain errors."""

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
