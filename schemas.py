from typing import Optional, List
from pydantic import BaseModel, Field

class HealthResponse(BaseModel):
    status: str = Field("ok", description="Service health status")
    mode: str = Field(..., description="'live' or 'mock'")
    authenticated: bool = Field(..., description="Whether valid caller credentials are recognized")
    user: Optional[str] = Field(None, description="Current user identifier if authenticated")

class BalanceResponse(BaseModel):
    budget_balance_ils: float = Field(..., description="Available budget balance in ILS")
    savings_balance_ils: Optional[float] = Field(None, description="Private savings balance in ILS, if available")
    phone_for_otp: Optional[str] = Field(None, description="Registered phone number where transfer OTP is sent")
    user_name: Optional[str] = Field(None, description="User full display name")
    user_id: Optional[str] = Field(None, description="User internal member/employee ID")

class RecipientItem(BaseModel):
    user_id: str = Field(..., description="Unique member identifier")
    user_hid: str = Field(..., description="System internal HID used for transfer payload")
    display_name: str = Field(..., description="Full member display name")
    department: Optional[str] = Field(None, description="Member branch/department if available")

class RecipientSearchResponse(BaseModel):
    results: List[RecipientItem] = Field(default_factory=list)

class TransactionItem(BaseModel):
    transaction_id: str = Field(..., description="Internal line transaction ID")
    display_id: str = Field(..., description="Formatted reference number (e.g. 12345/6789)")
    date: str = Field(..., description="Transaction execution or request date")
    type: str = Field(..., description="Transaction category/type description")
    counterparty: str = Field(..., description="Sender or Receiver display name")
    details: str = Field(..., description="Transaction memo/notes")
    amount_ils: float = Field(..., description="Transaction sum in ILS (positive credit or negative charge)")
    balance_ils: Optional[float] = Field(None, description="Remaining balance after transaction if available")
    status: str = Field(..., description="Status translation (e.g. אושר, ממתין, בוטל)")
    can_cancel: bool = Field(False, description="True if caller has permissions to cancel this line")
    can_approve: bool = Field(False, description="True if line is awaiting OTP/approval")

class TransactionsResponse(BaseModel):
    transactions: List[TransactionItem] = Field(default_factory=list)
    total: int = Field(..., description="Count of retrieved transactions")

class TransferRequest(BaseModel):
    recipient_hid: str = Field(..., description="Recipient internal HID obtained via /recipients/search")
    recipient_name: str = Field(..., description="Recipient display name for confirmation")
    amount_ils: float = Field(..., gt=0, description="Amount in ILS to transfer (must be > 0)")
    details_for_receiver: Optional[str] = Field("", description="Memo visible to receiver")
    details_for_sender: Optional[str] = Field("", description="Memo visible in sender's personal budget")
    transaction_type: int = Field(1, description="Transfer type: 1 = Regular transfer (זיכוי חבר)")

class TransferResponse(BaseModel):
    status: str = Field(..., description="'pending_otp', 'completed', or 'submitted'")
    transaction_id: Optional[str] = Field(None, description="Transaction reference ID")
    requires_otp: bool = Field(False, description="True if SMS verification code is required")
    message: str = Field(..., description="Status description or instructions")

class ApproveOtpRequest(BaseModel):
    transaction_id: str = Field(..., description="Transaction ID pending confirmation")
    otp_code: str = Field(..., min_length=1, description="SMS OTP code received on phone")

class ApproveOtpResponse(BaseModel):
    status: str = Field(..., description="'approved' or 'failed'")
    transaction_id: str = Field(..., description="Transaction reference number")
    message: str = Field(..., description="Result message")

class CancelTransactionResponse(BaseModel):
    status: str = Field("cancelled", description="Operation status")
    transaction_line_id: str = Field(..., description="Transaction line ID that was cancelled")
    message: str = Field(..., description="Confirmation message")

class PendingApprovalItem(BaseModel):
    transaction_id: str = Field(..., description="Transaction line ID")
    display_id: str = Field(..., description="Reference code")
    date: str = Field(..., description="Date requested")
    initiator: str = Field(..., description="Name of person requesting the charge")
    amount_ils: float = Field(..., description="Requested charge amount in ILS")
    details: str = Field(..., description="Memo/notes")

class PendingApprovalsResponse(BaseModel):
    items: List[PendingApprovalItem] = Field(default_factory=list)
    total: int = Field(..., description="Total pending approvals count")

class DeclineApprovalResponse(BaseModel):
    status: str = Field("declined", description="Operation status")
    transaction_id: str = Field(..., description="Transaction ID that was declined")
    message: str = Field(..., description="Confirmation message")

class AuthorizedUserItem(BaseModel):
    user_id: str = Field(..., description="User ID")
    user_name: str = Field(..., description="User display name")
    is_authorized: bool = Field(..., description="True if member is pre-authorized to charge your budget")

class AuthorizedUsersResponse(BaseModel):
    users: List[AuthorizedUserItem] = Field(default_factory=list)

class SetAuthorizedUserRequest(BaseModel):
    user_id: str = Field(..., description="User ID to update")
    user_name: str = Field(..., description="User display name")
    is_authorized: bool = Field(..., description="True to authorize, False to revoke authorization")

class SetAuthorizedUserResponse(BaseModel):
    status: str = Field("ok", description="Status of update")
    user_id: str = Field(..., description="User ID updated")
    is_authorized: bool = Field(..., description="New authorization state")

class ErrorDetail(BaseModel):
    code: str
    message: str

class ErrorResponse(BaseModel):
    error: ErrorDetail

class ReportTypeItem(BaseModel):
    id: int = Field(..., description="Report numeric ID")
    name: str = Field(..., description="Hebrew name of the report")
    description: str = Field(..., description="English description of the report content")
    parameter_type: str = Field(..., description="'monthly' (Year/FromMonth/ToMonth), 'date_range' (FromDate/ToDate), or 'none'")

class ReportTypesResponse(BaseModel):
    reports: List[ReportTypeItem] = Field(default_factory=list)

class ReportLineItem(BaseModel):
    date: Optional[str] = Field(None, description="Transaction date")
    document_no: Optional[str] = Field(None, description="Document/invoice reference number")
    details: str = Field(..., description="Item/charge description")
    debit_ils: float = Field(0.0, description="Debit amount (charge)")
    credit_ils: float = Field(0.0, description="Credit amount (allowance/refund)")
    quantity: float = Field(0.0, description="Quantity")
    month: Optional[str] = Field(None, description="Month/Year period")

class ReportSummary(BaseModel):
    opening_balance_ils: Optional[float] = None
    total_credits_ils: Optional[float] = None
    total_debits_ils: Optional[float] = None
    interest_ils: Optional[float] = None
    closing_balance_ils: Optional[float] = None

class ReportDataResponse(BaseModel):
    report_id: int
    report_name: str
    period: str
    summary: Optional[ReportSummary] = None
    items: List[ReportLineItem] = Field(default_factory=list)
    total_items: int
