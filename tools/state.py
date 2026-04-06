"""
Tracks AWS resources created during deploy for ordered teardown.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeploymentState:
    suffix: str
    rds_instance_id: str | None = None
    rds_sg_id: str | None = None
    db_subnet_group: str | None = None
    ecs_task_sg_id: str | None = None
    alb_sg_id: str | None = None
    alb_arn: str | None = None
    listener_arn: str | None = None
    target_group_arn: str | None = None
    cluster_name: str | None = None
    service_name: str | None = None
    ecr_repo_name: str | None = None
    execution_role_arn: str | None = None
    task_role_arn: str | None = None
    log_group_name: str | None = None
    task_definition_family: str | None = None
    dynamo_order_logs_table: str | None = None
    dynamo_courier_positions_table: str | None = None
    dynamo_order_logs_arn: str | None = None
    dynamo_courier_positions_arn: str | None = None
    created_sg_ids: list[str] = field(default_factory=list)

    def note_sg(self, sg_id: str) -> None:
        if sg_id not in self.created_sg_ids:
            self.created_sg_ids.append(sg_id)
