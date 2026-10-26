"""Wire models and error codes of the Backend API: the contract both services use.

They hold no behaviour beyond validation, so the gateway and the Backend API can
import them without importing each other.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

VERIFICATION_FAILED = "verification_failed"
SLOT_UNAVAILABLE = "slot_unavailable"
ALREADY_BOOKED = "already_booked"


class ContractModel(BaseModel):
    """Base model: unknown keys are always rejected."""

    model_config = ConfigDict(extra="forbid")


class SlotOption(ContractModel):
    """One offered technician window, wall-clock local time without a zone."""

    starts_at: StrictStr
    ends_at: StrictStr


class OrderRecord(ContractModel):
    """Allowlisted fictional order fields; never includes postcode or contact data."""

    order_number: StrictStr
    first_name: StrictStr
    item_names: list[StrictStr]
    status: Literal["active"]
    dispatch_date: StrictStr | None
    delivery_date: StrictStr | None


class Appointment(ContractModel):
    starts_at: StrictStr
    ends_at: StrictStr
    reference: StrictStr


class InstallationRecord(ContractModel):
    """Installation progress for one account; the visit is whatever is booked."""

    order_number: StrictStr
    stage: Literal["awaiting_visit", "visit_booked"]
    appointment: Appointment | None


class Availability(ContractModel):
    slots: list[SlotOption]


class BookingCommand(ContractModel):
    order_number: Annotated[StrictStr, Field(max_length=64)]
    starts_at: Annotated[StrictStr, Field(max_length=32)]


class OrderVerificationCommand(ContractModel):
    """Internal verification body; only the Backend API sees postcode and code."""

    order_number: StrictStr | None
    delivery_postcode: Annotated[StrictStr, Field(max_length=64)]
    verification_code: Annotated[StrictStr, Field(max_length=64)]


class OrderVerificationResult(ContractModel):
    """Public verification outcome: one boolean and one indistinguishable error."""

    verified: StrictBool
    error: Literal["verification_failed"] | None

    @model_validator(mode="after")
    def _consistent(self) -> "OrderVerificationResult":
        if self.verified and self.error is not None:
            raise ValueError("a verified result cannot carry an error")
        if not self.verified and self.error != VERIFICATION_FAILED:
            raise ValueError("an unverified result must use verification_failed")
        return self
