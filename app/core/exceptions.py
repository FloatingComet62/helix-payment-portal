from fastapi import HTTPException, status


class FestNotFoundError(HTTPException):
    def __init__(self, fest_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fest '{fest_id}' not found.",
        )


class FestAlreadyExistsError(HTTPException):
    def __init__(self, fest_id: str):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Fest '{fest_id}' already exists.",
        )


class FestHasTransactionsError(HTTPException):
    def __init__(self, fest_id: str):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete fest '{fest_id}' because it has associated transactions.",
        )


class TransactionNotFoundError(HTTPException):
    def __init__(self, tx_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction '{tx_id}' not found.",
        )


class InvalidHashError(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hash verification failed. Possible tampered request.",
        )


class AdminAuthError(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


class CouponValidationError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Coupon validation failed: {detail}",
        )
