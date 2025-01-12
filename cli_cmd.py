from itertools import cycle
from textwrap import wrap
from typing import Optional
from settings_model import Settings
from utils import redis_conn
from utils import redis_keys
import typer
import redis
import json
from rich.console import Console
from rich.table import Table
from rich.text import Text
from datetime import datetime
from rich.pretty import pretty_repr

import re

console = Console()

REDIS_CONN_CONF = redis_conn.REDIS_CONN_CONF
app = typer.Typer()


@app.command()
def projectStatus(namespace: str = typer.Option("", "--namespace"), projectId: str = typer.Option(None, "--projectId")):
    r = redis.Redis(**REDIS_CONN_CONF, single_connection_client=True)

    index_status = None
    key = 'projects:IndexStatus'
    if projectId:
        index_status = r.hget(key, projectId)
        if not index_status:
            console.log(f"\n[bold red]Project is not indexed[bold red]: \n{projectId}\n")
            return
        index_status = [{projectId: index_status.decode('utf-8')}]
    else:
        index_status = r.hgetall(key)
        if not index_status:
            console.log("\n[bold red]Indexes map doesn't exist [bold red]: 'projects:IndexStatus'\n")
            return
        index_status = dict(filter(lambda elem: namespace in elem[0].decode('utf-8'), index_status.items()))
        index_status = [{k.decode('utf-8'): v.decode('utf-8')} for k, v in index_status.items()]

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("ProjectId", justify="center")
    table.add_column("First Epoch Start Height", justify="center")
    table.add_column("Current Epoch End Height", justify="center")
    table.add_column("Dag chain issues", justify="center")
    table.add_column("Epochs pending finalization", justify="center")

    for project_indexes in index_status:
        k, v = project_indexes.popitem()
        v = json.loads(v)

        chain_issues_key = f"projectID:{k}:dagChainGaps"
        chain_issues = r.zrangebyscore(
            chain_issues_key,
            min='-inf',
            max='+inf',
            withscores=True)

        finalized_height_key = f"projectID:{k}:blockHeight"
        finalized_height=r.get(finalized_height_key)

        payload_cids_key = f"projectID:{k}:payloadCids"
        payload_cids = r.zrangebyscore(
            payload_cids_key,
            min=finalized_height,
            max='+inf',
            withscores=True)
        unfinalized_epochs = int(len(payload_cids)/2)

        table.add_row(
            Text(k, justify="left", overflow="ellipsis"),
            str(v["startSourceChainHeight"]),
            str(v["currentSourceChainHeight"]),
            str(int(len(chain_issues)/2)),
            str(unfinalized_epochs)
        )

    console.print(table)


@app.command()
def pruning_cycles_status(cycles: int = typer.Option(3, "--cycles")):

    r = redis.Redis(**REDIS_CONN_CONF, single_connection_client=True)

    cycles = 20 if cycles > 20 else cycles
    cycles = 3 if cycles < 1 else cycles

    pruningStatusZset = r.zrangebyscore(
        name='pruningRunStatus',
        min='-inf',
        max='+inf',
        withscores=True
    )

    table = Table(show_header=True, header_style="bold magenta", show_lines=True )
    table.add_column("Timestamp", justify="center", vertical="middle")
    table.add_column("Cycle status", justify="center")

    for entry in reversed(pruningStatusZset):
        payload, timestamp = entry
        payload = json.loads(payload.decode('utf-8')) if payload else {}
        timestamp = int(timestamp/1000) if timestamp and len(str(int(timestamp))) > 10  else timestamp

        payload_text = Text()
        payload_text.append(f"pruningCycleID: {payload.get('pruningCycleID')}\n")
        payload_text.append(f"cycleStartTime: {payload.get('cycleStartTime')}\n")
        payload_text.append(f"cycleEndTime: {payload.get('cycleEndTime')}\n")
        payload_text.append(f"projectsCount: {payload.get('projectsCount')}\n")
        payload_text.append(f"projectsProcessSuccessCount: {payload.get('projectsProcessSuccessCount')}\n", style="bold green")
        payload_text.append(f"projectsProcessFailedCount: {payload.get('projectsProcessFailedCount')}\n", style="bold red")
        payload_text.append(f"projectsNotProcessedCount: {payload.get('projectsNotProcessedCount')}", style="bold yellow")

        table.add_row(
            Text(f"{datetime.fromtimestamp(timestamp)} ( {timestamp} )"),
            payload_text
        )

        cycles -= 1
        if cycles <= 0:
            break

    console.print(table)


@app.command()
def pruning_cycle_project_report(cycleId: str = typer.Option(None, "--cycleId")):

    r = redis.Redis(**REDIS_CONN_CONF, single_connection_client=True)
    cycleDetails = {}

    if not cycleId:
        cycleDetails = r.zrevrange(
            name='pruningRunStatus',
            start=0,
            end=0
        )
        cycleDetails = json.loads(cycleDetails[0].decode('utf-8')) if len(cycleDetails)>0 else {}
    else:
        allCycles = r.zrangebyscore(
            name='pruningRunStatus',
            min='-inf',
            max='+inf'
        )
        for cycle in allCycles:
            cycle = json.loads(cycle.decode('utf-8')) if cycle else {}
            if cycle.get('pruningCycleID', None) == cycleId:
                cycleDetails = cycle
                break

    cycleStartTime = cycleDetails.get('cycleStartTime', None)
    cycleStartTime = int(cycleStartTime/1000) if cycleStartTime and len(str(int(cycleStartTime))) > 10  else cycleStartTime
    cycleEndTime = cycleDetails.get('cycleEndTime', None)
    cycleEndTime = int(cycleEndTime/1000) if cycleEndTime and len(str(int(cycleEndTime))) > 10  else cycleEndTime

    allProjectsDetails = r.hgetall(f"pruningProjectDetails:{cycleDetails.get('pruningCycleID', None)}")
    if not allProjectsDetails:
        console.print(f"[bold red]Can't find project details- pruningProjectDetails:{cycleDetails.get('pruningCycleID', None)}[/bold red]\n")
    else:
        table = Table(show_header=True, header_style="bold magenta", show_lines=True )
        table.add_column("ProjectId", overflow="fold", justify="center", vertical="middle")
        table.add_column("Details", justify="center")

        for projectId, projectDetails in allProjectsDetails.items():
            projectId = projectId.decode('utf-8') if projectId else {}
            projectDetails = json.loads(projectDetails.decode('utf-8')) if projectDetails else {}

            payload_text = Text()
            payload_text.append(f"DAGSegmentsProcessed: {projectDetails.get('DAGSegmentsProcessed')}\n", style="bold green")
            payload_text.append(f"DAGSegmentsArchived: {projectDetails.get('DAGSegmentsArchived')}\n", style="bold green")
            payload_text.append(f"CIDsUnPinned: {projectDetails.get('CIDsUnPinned')}\n")
            if projectDetails.get('DAGSegmentsArchivalFailed', False):
                payload_text.append(f"DAGSegmentsArchivalFailed: {projectDetails.get('DAGSegmentsArchivalFailed')}\n", style="bold red")
            if projectDetails.get('failureCause', False):
                payload_text.append(f"failureCause: {projectDetails.get('failureCause')}\n", style="bold red")
            if projectDetails.get('unPinFailed', False):
                payload_text.append(f"unPinFailed: {projectDetails.get('unPinFailed')}\n", style="bold red")

            table.add_row(
                Text(projectId, style='bright_cyan'),
                payload_text
            )

        console.print(table)


    console.print("\n\n[bold magenta]Pruning cycleId:[/bold magenta]", f"[bold bright_cyan]{cycleDetails.get('pruningCycleID', None)}[/bold bright_cyan]")
    console.print("[bold magenta]Start timestamp:[/bold magenta]", f"[white] {datetime.fromtimestamp(cycleStartTime)} ( {cycleStartTime} )[/white]")
    console.print("[bold magenta]End timestamp:[/bold magenta]", f"[white]{datetime.fromtimestamp(cycleEndTime)} ( {cycleEndTime} )[/white]")
    console.print("[bold blue]Projects count:[/bold blue]", f"[bold blue]{cycleDetails.get('projectsCount', None)}[/bold blue]")
    console.print("[bold green]Success count:[/bold green]", f"[bold green]{cycleDetails.get('projectsProcessSuccessCount', None)}[/bold green]")
    console.print("[bold red]Failure counts:[/bold red]", f"[bold red]{cycleDetails.get('projectsProcessFailedCount', None)}[/bold red]")
    console.print("[bold yellow]Unprocessed Project count:[/bold yellow]", f"[bold yellow]{cycleDetails.get('projectsNotProcessedCount', None)}[/bold yellow]\n\n")


@app.command()
def skip_pair_projects_verified_heights():
    r = redis.Redis(**REDIS_CONN_CONF, single_connection_client=True)

    print("\nThis command will force-push Summary project's redis state ahead.\n ")
    count = 0

    verification_status_key = f"projects:dagVerificationStatus"
    projects = r.hgetall(verification_status_key)
    console.log("project count:",len(projects))
    for project,verified_height in projects.items():
        project_str = project.decode('utf-8')
        if project_str.find('Snapshot') > 0:
            console.log("Found project which is Summary project",project_str, " and skipping it")
            continue
        block_height_key = f"projectID:{project_str}:blockHeight"
        console.log("Project Id is ",project_str)
        project_height = r.get(block_height_key)
        project_height = project_height.decode('utf-8')
        console.log("Project height is ",project_height)
        project_height = int(project_height)
        if project_height > int(verified_height)+10:
            console.log("difference in height for project %s is %s",project_str, (project_height - int(verified_height)))
            count+=1
            projects[project] = int(verified_height)+4
    if count > 0:
        all([r.hset(verification_status_key, k, v) for k, v in projects.items()])
        console.log("updated project verification heights successfully for %d projects",count)
    else:
        console.log("No need to update project verification heights as all projects have been verified till their current height.")


if __name__ == '__main__':
    app()
