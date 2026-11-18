
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from Engine.parser.parser import parse_sql_to_steps


app = FastAPI(title="ECHO API")


class InvestigateRequest(BaseModel):
    sql: str


class StepOut(BaseModel):
    step_id: int
    step_type: str
    name: str
    rows: Optional[int] = None
    total: Optional[float] = None
    suspicious: bool = False
    note: Optional[str] = None


class InvestigationOut(BaseModel):
    case_id: int
    sql: str
    steps: list[StepOut]
    first_suspicious_step: Optional[int] = None
    bug_type: Optional[str] = None
    explanation: Optional[str] = None
    is_demo: bool = False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/investigate", response_model=InvestigationOut)
def investigate(request: InvestigateRequest):
    try:
        parsed_steps = parse_sql_to_steps(request.sql)
    except (ValueError, SyntaxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    steps = [
        StepOut(
            step_id=step["step_id"],
            step_type=step["step_type"],
            name=step["name"],
            note=step.get("description"),
        )
        for step in parsed_steps
    ]

    return InvestigationOut(
        case_id=1,
        sql=request.sql,
        steps=steps,
        first_suspicious_step=None,
        bug_type=None,
        explanation=(
            "SQL structure parsed successfully. "
            "Execution-based bug detection is not connected yet."
        ),
        is_demo=False,
    )


@app.get("/cases")
def list_cases():
    return []
