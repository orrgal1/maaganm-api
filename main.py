import logging
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, Depends, Header, Request, status, Query
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError

import config
from schemas import (
    HealthResponse,
    BalanceResponse,
    RecipientSearchResponse,
    RecipientItem,
    TransactionsResponse,
    TransactionItem,
    TransferRequest,
    TransferResponse,
    ApproveOtpRequest,
    ApproveOtpResponse,
    CancelTransactionResponse,
    PendingApprovalsResponse,
    PendingApprovalItem,
    DeclineApprovalResponse,
    AuthorizedUsersResponse,
    AuthorizedUserItem,
    SetAuthorizedUserRequest,
    SetAuthorizedUserResponse,
    ErrorResponse,
    ReportTypesResponse,
    ReportTypeItem,
    ReportDataResponse
)
from security import (
    verify_bearer_token,
    get_budget_credentials,
    BudgetCredentials,
    APIException,
    sanitize_log_message
)
from idempotency import idempotency_store
from budget_driver import budget_driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("maaganm_api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Maagan Michael Budget service...")
    yield
    logger.info("Shutting down Maagan Michael Budget service...")
    await budget_driver.close()

app = FastAPI(
    title="Maagan Michael Budget Local API Wrapper",
    description="Local authenticated API wrapping Kibbutz Maagan Michael budget portal (budget.mmm.org.il)",
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_bearer_token)]
)

# ==================== Exception Handlers ====================

@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": {"code": "invalid_input", "message": str(exc)}}
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled server error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "upstream_error", "message": f"Service encountered an error: {str(exc)}"}}
    )

# ==================== Endpoints ====================

@app.get("/health", response_model=HealthResponse)
async def get_health(
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /health
    Returns service health, operating mode (live vs mock), and caller auth status.
    """
    is_authed, user_id = await budget_driver.check_login_status(creds.username, creds.password)
    return HealthResponse(
        status="ok",
        mode="mock" if config.MOCK_MODE else "live",
        authenticated=is_authed,
        user=user_id if is_authed else None
    )

@app.get("/balance", response_model=BalanceResponse)
async def get_balance(
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /balance
    Fetches the caller's available personal budget balance and private savings balance.
    """
    balance = await budget_driver.get_balance(creds.username, creds.password)
    return BalanceResponse(**balance)

@app.get("/recipients/search", response_model=RecipientSearchResponse)
async def search_recipients(
    q: str = Query("", description="Search query by name or member ID"),
    transaction_type: int = Query(1, description="Transaction type: 1 = regular transfer"),
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /recipients/search?q=<name_or_id>
    Searches kibbutz members directory for transfer recipient selection.
    """
    results = await budget_driver.search_recipients(
        creds.username,
        creds.password,
        query=q,
        transaction_type=transaction_type
    )
    return RecipientSearchResponse(results=[RecipientItem(**r) for r in results])

@app.get("/transactions", response_model=TransactionsResponse)
async def get_transactions(
    from_date: Optional[str] = Query(None, description="Start date (DD/MM/YYYY)"),
    to_date: Optional[str] = Query(None, description="End date (DD/MM/YYYY)"),
    types: Optional[str] = Query(None, description="Comma-separated transaction types (e.g. 1,2,3,4)"),
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /transactions?from_date=01/01/2026&to_date=20/09/2026
    Retrieves the caller's transaction history with status, amounts, and cancellation eligibility.
    """
    transactions = await budget_driver.get_transactions(
        creds.username,
        creds.password,
        from_date=from_date,
        to_date=to_date,
        types=types
    )
    return TransactionsResponse(
        transactions=[TransactionItem(**t) for t in transactions],
        total=len(transactions)
    )

@app.post("/transfer", response_model=TransferResponse)
async def transfer_money(
    payload: TransferRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    POST /transfer
    Submits a money transfer to another kibbutz member.
    If the system requires SMS OTP confirmation, response will have requires_otp=True and a transaction_id.
    """
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    result = await budget_driver.transfer(
        username=creds.username,
        password=creds.password,
        recipient_hid=payload.recipient_hid,
        recipient_name=payload.recipient_name,
        amount_ils=payload.amount_ils,
        details_receiver=payload.details_for_receiver or "",
        details_sender=payload.details_for_sender or "",
        transaction_type=payload.transaction_type
    )

    if idempotency_key:
        idempotency_store.set(idempotency_key, status.HTTP_200_OK, result)

    return TransferResponse(**result)

@app.post("/transfer/approve", response_model=ApproveOtpResponse)
async def approve_transfer_otp(
    payload: ApproveOtpRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    POST /transfer/approve
    Confirms a staged transfer using the SMS OTP code received on the registered mobile phone.
    """
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    result = await budget_driver.approve_otp(
        username=creds.username,
        password=creds.password,
        transaction_id=payload.transaction_id,
        otp_code=payload.otp_code
    )

    if idempotency_key:
        idempotency_store.set(idempotency_key, status.HTTP_200_OK, result)

    return ApproveOtpResponse(**result)

@app.delete("/transactions/{transaction_line_id}", response_model=CancelTransactionResponse)
async def cancel_transaction(
    transaction_line_id: str,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    DELETE /transactions/{transaction_line_id}
    Cancels an eligible pending or recent transaction line.
    """
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    result = await budget_driver.cancel_transaction(
        username=creds.username,
        password=creds.password,
        transaction_line_id=transaction_line_id
    )

    if idempotency_key:
        idempotency_store.set(idempotency_key, status.HTTP_200_OK, result)

    return CancelTransactionResponse(**result)

@app.get("/approvals/pending", response_model=PendingApprovalsResponse)
async def get_pending_approvals(
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /approvals/pending
    Lists incoming charge requests waiting for your approval.
    """
    items = await budget_driver.get_pending_approvals(creds.username, creds.password)
    return PendingApprovalsResponse(
        items=[PendingApprovalItem(**i) for i in items],
        total=len(items)
    )

@app.delete("/approvals/{transaction_line_id}", response_model=DeclineApprovalResponse)
async def decline_approval(
    transaction_line_id: str,
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    DELETE /approvals/{transaction_line_id}
    Declines an incoming charge request.
    """
    result = await budget_driver.decline_pending_approval(
        username=creds.username,
        password=creds.password,
        transaction_line_id=transaction_line_id
    )
    return DeclineApprovalResponse(**result)

@app.get("/account/authorized-users", response_model=AuthorizedUsersResponse)
async def get_authorized_users(
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /account/authorized-users
    Returns list of members authorized to charge your budget account directly.
    """
    users = await budget_driver.get_authorized_users(creds.username, creds.password)
    return AuthorizedUsersResponse(users=[AuthorizedUserItem(**u) for u in users])

@app.put("/account/authorized-users", response_model=SetAuthorizedUserResponse)
async def set_authorized_user(
    payload: SetAuthorizedUserRequest,
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    PUT /account/authorized-users
    Grants or revokes direct-charge authorization for a specific kibbutz member.
    """
    result = await budget_driver.set_authorized_user(
        username=creds.username,
        password=creds.password,
        user_id=payload.user_id,
        user_name=payload.user_name,
        is_authorized=payload.is_authorized
    )
    return SetAuthorizedUserResponse(**result)

@app.get("/reports/types", response_model=ReportTypesResponse)
async def get_report_types():
    """
    GET /reports/types
    Returns catalogue of available budget reports with their parameter specifications.
    """
    types = budget_driver.get_report_types()
    return ReportTypesResponse(reports=[ReportTypeItem(**t) for t in types])

@app.get("/reports/generate")
async def generate_report(
    report_id: int = Query(1, description="Report numeric ID (see /reports/types)"),
    format: str = Query("json", description="Output format: 'json', 'csv', 'pdf', or 'xls'"),
    year: Optional[int] = Query(None, description="Year for monthly reports (e.g. 2026)"),
    from_month: Optional[int] = Query(None, description="Starting month (1-12)"),
    to_month: Optional[int] = Query(None, description="Ending month (1-12)"),
    from_date: Optional[str] = Query(None, description="Start date (DD/MM/YYYY) for date-range reports"),
    to_date: Optional[str] = Query(None, description="End date (DD/MM/YYYY) for date-range reports"),
    creds: BudgetCredentials = Depends(get_budget_credentials)
):
    """
    GET /reports/generate
    Generates and exports reports from budget.mmm.org.il.
    When format='json', returns structured summary and line items.
    When format='csv', 'pdf', or 'xls', returns the binary download.
    """
    data, media_type = await budget_driver.generate_report(
        username=creds.username,
        password=creds.password,
        report_id=report_id,
        format=format.lower(),
        year=year,
        from_month=from_month,
        to_month=to_month,
        from_date=from_date,
        to_date=to_date
    )

    if format.lower() == "json":
        return JSONResponse(content=data)

    filename = f"report_{report_id}_{format}.{format}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
