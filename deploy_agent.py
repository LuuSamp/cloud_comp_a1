"""
Legacy entry point — agent runs on Learner Lab, not a separate Bedrock account.

Use:
  python deploy.py --skip-teardown --with-agent
  python deploy.py --service agent

Bedrock API keys belong in `.env.agent` (injected as BEDROCK_* on the lab ECS task).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.agent_deploy import AGENT_SERVICE_ID


def main() -> None:
    parser = argparse.ArgumentParser(
        description="(Deprecated) Use deploy.py --with-agent for lab-hosted agent"
    )
    parser.add_argument("--service", choices=[AGENT_SERVICE_ID])
    parser.add_argument("--skip-teardown", action="store_true")
    parser.add_argument("--teardown-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    print(
        "[deploy_agent] This script no longer deploys infra to the Bedrock credits account.\n"
        "  Full stack + agent on lab ALB:\n"
        "    python deploy.py --skip-teardown --with-agent\n"
        "  Redeploy agent image only:\n"
        "    python deploy.py --service agent\n"
        "  Optional UI on same ALB:\n"
        "    python deploy_agent_ui.py\n"
    )

    if args.teardown_only or args.resume or args.skip_teardown:
        print(
            "[deploy_agent] Use deploy.py for teardown/resume "
            "(python deploy.py --teardown-only or --resume --with-agent)."
        )
        raise SystemExit(1)

    if args.service == AGENT_SERVICE_ID:
        from deploy import redeploy_single_service

        redeploy_single_service(_PROJECT_ROOT, AGENT_SERVICE_ID)
        return

    raise SystemExit(1)


if __name__ == "__main__":
    main()
