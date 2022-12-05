"""
# Configuring SQLMesh

You can configure your project in multiple places and SQLMesh will prioritize configurations according to
the following order of precedence.

From least to greatest precedence:
- A Config object defined in a config.py file at the root of your project.
    ```python
    # config.py
    import duckdb
    from sqlmesh.core.engine_adapter import EngineAdapter
    local_config = Config(
        engine_adapter=EngineAdapter(duckdb.connect(), "duckdb"),
        dialect="duckdb"
    )
    # End config.py

    >>> from sqlmesh import Context
    >>> context = Context(path="example", config="local_config")

    ```
- A Config object used when initializing a Context.
    ```python
    >>> from sqlmesh import Context
    >>> from sqlmesh.core.config import Config
    >>> my_config = Config(
    ...     engine_adapter=EngineAdapter(duckdb.connect(), "duckdb"),
    ...     dialect="duckdb"
    ... )
    >>> context = Context(path="example", config=my_config)

    ```
- Individual config parameters used when initializing a Context.
    ```python
    >>> adapter = EngineAdapter(duckdb.connect(), "duckdb")
    >>> context = Context(
    ...     path="example", engine_adapter=adapter,
    ...     dialect="duckdb",
    ... )

    ```

# Using Config

The most common way to configure your SQLMesh project is with a `config.py` module at the root of the
project.  A SQLMesh Context will automatically look for Config objects there. You can have multiple
Config objects defined and then tell Context which one to use. For example, you can have different
Configs for local and production environments, Airflow, and Model tests.

Example config.py:
```python
import duckdb

from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.config import Config, AirflowSchedulerBackend

from my_project.utils import load_test_data


DEFAULT_KWARGS = {
    "dialect": "duckdb", # The default dialect of models is DuckDB SQL.
    "engine_adapter": EngineAdapter(duckdb.connect(), "duckdb"), # The default engine runs in DuckDB.
}

# An in memory DuckDB config.
config = Config(**DEFAULT_KWARGS)

# A stateful DuckDB config.
local_config = Config(**{
    **DEFAULT_KWARGS,
    "engine_adapter": EngineAdapter(
        lambda: duckdb.connect(database="local.duckdb"), "duckdb"
    ),
})

# The config to run model tests.
test_config = Config(
    "on_init": load_test_data,
    **DEFAULT_KWARGS,
)

# A config that uses Airflow
airflow_config = Config(
    "scheduler_backend": AirflowSchedulerBackend(),
    **DEFAULT_KWARGS,
)
```

To use a Config, pass in its variable name to Context.
```python
>>> from sqlmesh import Context
>>> context = Context(path="example", config="local_config")

```

For more information about the Config class and its parameters, see `sqlmesh.core.config.Config`.
"""
from __future__ import annotations

import abc
import typing as t

import duckdb
from pydantic import Field
from requests import Session

from sqlmesh.core import constants as c
from sqlmesh.core._typing import NotificationTarget
from sqlmesh.core.console import Console
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.plan_evaluator import (
    AirflowPlanEvaluator,
    BuiltInPlanEvaluator,
    PlanEvaluator,
)
from sqlmesh.core.state_sync import EngineAdapterStateSync, StateReader, StateSync
from sqlmesh.schedulers.airflow.client import AirflowClient
from sqlmesh.schedulers.airflow.common import AIRFLOW_LOCAL_URL
from sqlmesh.utils.pydantic import PydanticModel

if t.TYPE_CHECKING:
    from sqlmesh.core.context import Context


class SchedulerBackend(abc.ABC):
    """Abstract base class for Scheduler configurations."""

    @abc.abstractmethod
    def create_plan_evaluator(self, context: Context) -> PlanEvaluator:
        """Creates a Plan Evaluator instance.

        Args:
            context: The SQLMesh Context.
        """

    def create_state_sync(self, context: Context) -> t.Optional[StateSync]:
        """Creates a State Sync instance.

        Args:
            context: The SQLMesh Context.

        Returns:
            The StateSync instance.
        """
        return None

    def create_state_reader(self, context: Context) -> t.Optional[StateReader]:
        """Creates a State Reader instance.

        Functionality related to evaluation on a client side (Context.evaluate, Context.run, etc.)
        will be unavailable if a State Reader instance is available but a State Sync instance is not.

        Args:
            context: The SQLMesh Context.

        Returns:
            The StateReader instance.
        """
        return None


class BuiltInSchedulerBackend(SchedulerBackend):
    """The Built-In Scheduler configuration."""

    def create_state_sync(self, context: Context) -> t.Optional[StateSync]:
        return EngineAdapterStateSync(
            context.engine_adapter, context.physical_schema, context.table_info_cache
        )

    def create_plan_evaluator(self, context: Context) -> PlanEvaluator:
        return BuiltInPlanEvaluator(
            state_sync=context.state_sync,
            snapshot_evaluator=context.snapshot_evaluator,
            console=context.console,
        )


class AirflowSchedulerBackend(SchedulerBackend, PydanticModel):
    """The Airflow Scheduler configuration."""

    airflow_url: str = AIRFLOW_LOCAL_URL
    username: str = "airflow"
    password: str = "airflow"
    max_concurrent_requests: int = 2
    dag_run_poll_interval_secs: int = 10
    dag_creation_poll_interval_secs: int = 30
    dag_creation_max_retry_attempts: int = 10

    def get_client(self, console: t.Optional[Console] = None) -> AirflowClient:
        session = Session()
        session.headers.update({"Content-Type": "application/json"})
        session.auth = (self.username, self.password)

        return AirflowClient(
            session=session,
            airflow_url=self.airflow_url,
            console=console,
        )

    def create_state_reader(self, context: Context) -> t.Optional[StateReader]:
        from sqlmesh.schedulers.airflow.state_sync import HttpStateReader

        return HttpStateReader(
            table_info_cache=context.table_info_cache,
            client=self.get_client(context.console),
            max_concurrent_requests=self.max_concurrent_requests,
            dag_run_poll_interval_secs=self.dag_run_poll_interval_secs,
            console=context.console,
        )

    def create_plan_evaluator(self, context: Context) -> PlanEvaluator:
        return AirflowPlanEvaluator(
            airflow_client=self.get_client(context.console),
            dag_run_poll_interval_secs=self.dag_run_poll_interval_secs,
            dag_creation_poll_interval_secs=self.dag_creation_poll_interval_secs,
            dag_creation_max_retry_attempts=self.dag_creation_max_retry_attempts,
            console=context.console,
            notification_targets=context.notification_targets,
        )


class CloudComposerSchedulerBackend(AirflowSchedulerBackend, PydanticModel):
    from google.auth.transport.requests import AuthorizedSession

    airflow_url: str
    max_concurrent_requests: int = 2
    dag_run_poll_interval_secs: int = 10
    dag_creation_poll_interval_secs: int = 30
    dag_creation_max_retry_attempts: int = 10
    _session: t.Optional[AuthorizedSession] = None

    @property
    def session(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        if self._session is None:
            self._session = AuthorizedSession(
                google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )[0]
            )
        return self._session

    def get_client(self, console: t.Optional[Console] = None) -> AirflowClient:
        return AirflowClient(
            airflow_url=self.airflow_url,
            session=self.session,
            console=console,
        )


class Config(PydanticModel):
    """
    An object used by a Context to configure your SQLMesh project.

    An engine adapter can lazily establish a database connection if it is passed a callable that returns a
    database API compliant connection.
    ```python
    >>> from sqlmesh import Context
    >>> context = Context(
    ...     path="example",
    ...     engine_adapter=EngineAdapter(duckdb.connect, "duckdb"),
    ...     dialect="duckdb"
    ... )

    ```
    ```python
    >>> def create_connection():
    ...     return duckdb.connect()
    ...
    >>> context = Context(
    ...     path="example",
    ...     engine_adapter=EngineAdapter(create_connection, "duckdb"),
    ...     dialect="duckdb"
    ... )

    ```

    Args:
        engine_adapter: The default engine adapter to use
        scheduler_backend: Identifies which scheduler backend to use.
        notification_targets: The notification targets to use.
        dialect: The default sql dialect of model queries.
        physical_schema: The default schema used to store materialized tables.
        snapshot_ttl: Duration before unpromoted snapshots are removed.
        time_column_format: The default format to use for all model time columns. Defaults to %Y-%m-%d.
    """

    engine_adapter: EngineAdapter = Field(
        default_factory=lambda: EngineAdapter(duckdb.connect, "duckdb")
    )
    scheduler_backend: SchedulerBackend = BuiltInSchedulerBackend()
    notification_targets: t.List[NotificationTarget] = []
    dialect: str = ""
    physical_schema: str = ""
    snapshot_ttl: str = ""
    ignore_patterns: t.List[str] = []
    time_column_format: str = c.DEFAULT_TIME_COLUMN_FORMAT

    class Config:
        arbitrary_types_allowed = True
