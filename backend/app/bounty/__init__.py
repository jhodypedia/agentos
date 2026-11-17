"""Bounty platform integrations.

Only officially documented interfaces are implemented here. make.fun publishes
its agent contract at https://make.fun/skill.md and a public OpenAPI document at
https://make.fun/api/openapi.public.json; this package talks to exactly those
routes and nothing invented alongside them.
"""

from app.bounty.makefun import MakeFunClient, MakeFunError, MakeFunProblem

__all__ = ["MakeFunClient", "MakeFunError", "MakeFunProblem"]
