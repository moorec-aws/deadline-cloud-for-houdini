# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys
import yaml
import json
from typing import Any
from pathlib import Path

from deadline.client.job_bundle._yaml import deadline_yaml_dump
from deadline.client.job_bundle.adaptors import (
    parse_frame_range,
)
from deadline.client import api
from deadline.client.job_bundle.submission import AssetReferences
from deadline.client.job_bundle import create_job_history_bundle_dir
from deadline.client.config import get_setting
from deadline.client.config.config_file import str2bool
from deadline.client.ui.dialogs.submit_job_progress_dialog import SubmitJobProgressDialog
from deadline.client.ui.dialogs import DeadlineConfigDialog, DeadlineLoginDialog
from deadline.job_attachments.upload import S3AssetManager
from deadline.job_attachments.models import JobAttachmentS3Settings

from .queue_parameters import update_queue_parameters, get_queue_parameter_values_as_openjd

import hou

IGNORE_REF_VALUES = ("opdef:", "oplib:", "temp:")
IGNORE_REF_PARMS = ("taskgraphfile", "pdg_workingdir", "soho_program")


def _get_hip_file() -> str:
    return hou.hipFile.path()


def _get_houdini_version() -> str:
    return hou.applicationVersionString()


def _get_scene_asset_references(rop_node: hou.Node) -> AssetReferences:
    asset_references = AssetReferences()
    input_filenames: set[str] = set()
    input_filenames.add(_get_hip_file())
    for n in hou.fileReferences():
        if n[0]:
            if n[0].node() == rop_node:
                continue
            if n[1].startswith(IGNORE_REF_VALUES):
                continue
            if n[0].name() in IGNORE_REF_PARMS:
                continue
        if os.path.isdir(n[1]):
            continue
        input_filenames.add(n[1])
    rop_steps = _get_rop_steps(rop_node)
    rop_dir_map = {
        "Driver/ifd": "vm_picture",
        "Driver/karma": "picture",
        "Driver/geometry": "sopoutput",
        "Driver/alembic": "filename",
        "Sop/filecache": "file",
        "Sop/rop_geometry": "sopoutput",
        "Sop/rop_alembic": "filename",
    }
    output_directories: set[str] = set()
    for n in rop_steps:
        node = hou.node(n["rop"])
        type_name = node.type().nameWithCategory()
        out_parm = rop_dir_map.get(type_name, None)
        if out_parm is not None:
            path = node.parm(out_parm).eval()
            if path:
                output_directories.add(os.path.dirname(path))
    asset_references.input_filenames = input_filenames
    asset_references.output_directories = output_directories
    return asset_references


def _get_rop_steps(rop: hou.Node):
    """
    Parse hscript render command output to steps
    https://www.sidefx.com/docs/houdini/commands/render.html

    Format:
    ```
    <<id>> [ <<dependencies>> ] <<node>> ( <<frames>> )
    ```
    """
    cmd = "render -p -c -F %s" % rop.path()
    out, err = hou.hscript(cmd)
    if err:
        raise Exception("hscript render: failed to list steps\n\n{}".format(err))
    rop_steps: list[dict[str, Any]] = []
    for n in out.split("\n"):
        if not n.strip():
            continue
        # two parts: rops and frame notation
        parts = n.split("\t")
        # id [ deps ] node
        rop_str = parts[0]
        # frames
        frange_part = parts[1]
        frange = frange_part.replace("( ", "")
        frange = frange.replace(" )", "")
        range_parts = frange.split(" ")
        range_ints = [int(f) for f in range_parts]
        # handle single frame
        if len(range_ints) == 1:
            frame = range_ints[0]
            range_ints = [frame, frame, 1]
        rop_parts = rop_str.split(" ")
        # first token is the int id generated by hscript render
        _id = rop_parts[0]
        # full path to rop
        path = rop_parts[-2]
        # section after id lists the dependencies between [ ]
        deps: list[str] = []
        for d in rop_parts[1:-2]:
            if d in ["[", "]"]:
                continue
            # id this depends on
            deps.append(d)
        # skip deadline and deadline_cloud rops
        if hou.node(path).type().name() in ("deadline", "deadline_cloud"):
            continue
        step_dict = {
            "id": _id,
            "name": "%s-%s" % (path, _id),
            "deps": deps,
            "rop": path,
            "start": range_ints[0],
            "stop": range_ints[1],
            "step": range_ints[2],
        }
        rop_steps.append(step_dict)
    return rop_steps


def _get_parameter_values(node: hou.Node) -> dict[str, Any]:
    priority = node.parm("priority").eval()
    initial_status = node.parm("initial_status").evalAsString()
    failed_tasks_limit = node.parm("failed_tasks_limit").eval()
    task_retry_limit = node.parm("task_retry_limit").eval()
    return {
        "parameterValues": [
            {"name": "deadline:priority", "value": priority},
            {"name": "deadline:targetTaskRunStatus", "value": initial_status},
            {"name": "deadline:maxFailedTasksCount", "value": failed_tasks_limit},
            {"name": "deadline:maxRetriesPerTask", "value": task_retry_limit},
            *get_queue_parameter_values_as_openjd(node),
        ]
    }


def _get_job_template(rop: hou.Node) -> dict[str, Any]:
    job_name = rop.parm("name").evalAsString()
    job_description = rop.parm("description").evalAsString()
    separate_steps = rop.parm("separate_steps").eval()
    rop_steps = _get_rop_steps(rop)
    id_steps = {n["id"]: n for n in rop_steps}
    queue_parameter_definitions_json = rop.userData("queue_parameter_definitions")
    parameter_definitions: list[dict[str, Any]] = (
        json.loads(queue_parameter_definitions_json)
        if queue_parameter_definitions_json is not None
        else []
    )
    parameter_definitions.append(
        {
            "name": "HipFile",
            "type": "PATH",
            "objectType": "FILE",
            "dataFlow": "IN",
            "default": _get_hip_file(),
        }
    )
    steps: list[dict[str, Any]] = []
    ignore_input_nodes = "true"
    if not separate_steps:
        rop_steps = [rop_steps[0]]
        ignore_input_nodes = "false"
    for n in rop_steps:
        # init data
        init_data_contents = []
        init_data_contents.append("scene_file: '{{Param.HipFile}}'\n")
        init_data_contents.append("render_node: '%s'\n" % n["rop"])
        init_data_contents.append("version: %s\n" % _get_houdini_version())
        init_data_contents.append("ignore_input_nodes: %s\n" % ignore_input_nodes)
        init_data_attachment = {
            "name": "initData",
            "filename": "init-data.yaml",
            "type": "TEXT",
            "data": "".join(init_data_contents),
        }
        # environments
        environments = get_houdini_environments(init_data_attachment)
        # task run data
        task_data_contents = []
        task_data_contents.append("render_node: %s\n" % n["rop"])
        task_data_contents.append("frame: {{Task.Param.Frame}}\n")
        task_data_contents.append("ignore_input_nodes: true\n")
        # step
        frame_list = "{start}-{stop}:{step}".format(**n)
        step = {
            "name": n["name"],
            "parameterSpace": {
                "taskParameterDefinitions": [
                    {"name": "Frame", "range": parse_frame_range(frame_list), "type": "INT"}
                ]
            },
            "stepEnvironments": environments,
            "script": {
                "embeddedFiles": [
                    {
                        "name": "runData",
                        "filename": "run-data.yaml",
                        "type": "TEXT",
                        "data": "".join(task_data_contents),
                    },
                ],
                "actions": {
                    "onRun": {
                        "command": "HoudiniAdaptor",
                        "args": [
                            "daemon",
                            "run",
                            "--connection-file",
                            "{{ Session.WorkingDirectory }}/connection.json",
                            "--run-data",
                            "file://{{ Task.File.runData }}",
                        ],
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
                    },
                },
            },
        }
        if n["deps"]:
            deps = [{"dependsOn": id_steps[d]["name"]} for d in n["deps"]]
            step["dependencies"] = deps
        steps.append(step)
    job_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": job_name,
        "parameterDefinitions": parameter_definitions,
        "steps": steps,
    }
    if job_description:
        job_template["description"] = job_description
    include_adaptor_wheels = rop.parm("include_adaptor_wheels").eval()
    if include_adaptor_wheels:
        adaptor_wheels = rop.parm("adaptor_wheels").evalAsString()
        if os.path.exists(adaptor_wheels):
            override_file = os.path.join(
                os.path.dirname(__file__), "adaptor_override_environment.yaml"
            )
            with open(override_file) as yaml_file:
                override_environment = yaml.safe_load(yaml_file)
                override_environment["parameterDefinitions"][0]["default"] = str(adaptor_wheels)
                job_template["parameterDefinitions"].extend(
                    override_environment["parameterDefinitions"]
                )
                if "jobEnvironments" not in job_template:
                    job_template["jobEnvironments"] = []
                job_template["jobEnvironments"].append(override_environment["environment"])
    return job_template


def _get_asset_references(rop_node: hou.Node) -> AssetReferences:
    asset_references = AssetReferences()
    for n in rop_node.parm("input_filenames").multiParmInstances():
        asset_references.input_filenames.add(n.eval())
    for n in rop_node.parm("input_directories").multiParmInstances():
        asset_references.input_directories.add(n.eval())
    for n in rop_node.parm("output_directories").multiParmInstances():
        asset_references.output_directories.add(n.eval())
    return asset_references


def _create_job_bundle(
    rop_node: hou.Node, job_bundle_dir: str, asset_references: AssetReferences
) -> None:
    job_bundle_path = Path(job_bundle_dir)
    job_template = _get_job_template(rop_node)
    parameter_values = _get_parameter_values(rop_node)
    with open(job_bundle_path / "template.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(job_template, f, indent=1)
    with open(job_bundle_path / "parameter_values.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(parameter_values, f, indent=1)
    with open(job_bundle_path / "asset_references.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(asset_references.to_dict(), f, indent=1)


def p_parse_files(kwargs):
    node = kwargs["node"]
    asset_references = _get_scene_asset_references(node)
    for ref in ("input_filenames", "input_directories", "output_directories"):
        p = node.parm(ref)
        while p.multiParmInstancesCount():
            p.removeMultiParmInstance(0)
        paths = sorted(list(getattr(asset_references, ref)))
        p.set(len(paths))
        for i, n in enumerate(p.multiParmInstances()):
            n.set(paths[i])


def p_save_bundle(kwargs):
    node = kwargs["node"]
    name = node.parm("name").evalAsString()
    asset_references = _get_asset_references(node)
    try:
        job_bundle_dir = create_job_history_bundle_dir("houdini", name)
        _create_job_bundle(node, job_bundle_dir, asset_references)
        print("Saved the submission as a job bundle:")
        print(job_bundle_dir)
        if sys.platform == "win32":
            os.startfile(job_bundle_dir)
        hou.ui.displayMessage(
            "Saved the submission as a job bundle: %s" % job_bundle_dir,
            title="Houdini Job Submission",
        )
    except Exception as exc:
        print("Error saving bundle")
        hou.ui.displayMessage(
            str(exc), title="Houdini Job Submission", severity=hou.severityType.Warning
        )


def p_submit(kwargs):
    node = kwargs["node"]
    name = node.parm("name").evalAsString()
    # TODO: Populate from queue environment so that parameters can be overridden.
    queue_parameters: list[dict[str, Any]] = []
    asset_references = _get_asset_references(node)
    try:
        deadline = api.get_boto3_client("deadline")

        job_bundle_dir = create_job_history_bundle_dir("houdini", name)
        _create_job_bundle(node, job_bundle_dir, asset_references)

        farm_id = get_setting("defaults.farm_id")
        queue_id = get_setting("defaults.queue_id")
        storage_profile_id = get_setting("settings.storage_profile_id")

        queue = deadline.get_queue(farmId=farm_id, queueId=queue_id)

        queue_role_session = api.get_queue_user_boto3_session(
            deadline=deadline,
            farm_id=farm_id,
            queue_id=queue_id,
            queue_display_name=queue["displayName"],
        )

        asset_manager = S3AssetManager(
            farm_id=farm_id,
            queue_id=queue_id,
            job_attachment_settings=JobAttachmentS3Settings(**queue["jobAttachmentSettings"]),
            session=queue_role_session,
        )

        job_progress_dialog = SubmitJobProgressDialog(parent=hou.qt.mainWindow())
        job_progress_dialog.start_submission(
            farm_id,
            queue_id,
            storage_profile_id,
            job_bundle_dir,
            queue_parameters,
            asset_manager,
            deadline,
            auto_accept=str2bool(get_setting("settings.auto_accept")),
        )
    except Exception as exc:
        print(str(exc))
        hou.ui.displayMessage(
            str(exc), title="Houdini Job Submission", severity=hou.severityType.Warning
        )


def p_settings(kwargs):
    node = kwargs["node"]
    node.parm("farm").set("<refreshing>")
    node.parm("queue").set("<refreshing>")
    DeadlineConfigDialog.configure_settings(parent=hou.qt.mainWindow())
    deadline = api.get_boto3_client("deadline")
    farm_id = get_setting("defaults.farm_id")
    farm_response = deadline.get_farm(farmId=farm_id)
    node.parm("farm").set(farm_response["displayName"])
    queue_id = get_setting("defaults.queue_id")
    queue_response = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    node.parm("queue").set(queue_response["displayName"])
    update_queue_parameters(farm_id, queue_id, node)


def p_login(kwargs):
    node = kwargs["node"]
    node.parm("farm").set("<refreshing>")
    node.parm("queue").set("<refreshing>")
    DeadlineLoginDialog.login(parent=hou.qt.mainWindow())
    deadline = api.get_boto3_client("deadline")
    farm_id = get_setting("defaults.farm_id")
    farm_response = deadline.get_farm(farmId=farm_id)
    node.parm("farm").set(farm_response["displayName"])
    queue_id = get_setting("defaults.queue_id")
    queue_response = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    node.parm("queue").set(queue_response["displayName"])
    update_queue_parameters(farm_id, queue_id, node)


def p_logout(kwargs):
    node = kwargs["node"]
    node.parm("farm").set("")
    node.parm("queue").set("")
    api.logout()


def p_update_queue_parameters(kwargs):
    node = kwargs["node"]
    farm_id = get_setting("defaults.farm_id")
    queue_id = get_setting("defaults.queue_id")
    update_queue_parameters(farm_id, queue_id, node)


# TODO: remove this and swap to default job template
def get_houdini_environments(init_data_attachment: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of environments that set things up to run frame renders
    for the specified DCC.
    """
    return [
        {
            "name": "Houdini",
            "description": "Runs Houdini in the background.",
            "script": {
                "embeddedFiles": [
                    init_data_attachment,
                ],
                "actions": {
                    "onEnter": {
                        "command": "HoudiniAdaptor",
                        "args": [
                            "daemon",
                            "start",
                            "--path-mapping-rules",
                            "file://{{Session.PathMappingRulesFile}}",
                            "--connection-file",
                            "{{ Session.WorkingDirectory }}/connection.json",
                            "--init-data",
                            "file://{{ Env.File.initData }}",
                        ],
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
                    },
                    "onExit": {
                        "command": "HoudiniAdaptor",
                        "args": [
                            "daemon",
                            "stop",
                            "--connection-file",
                            "{{ Session.WorkingDirectory }}/connection.json",
                        ],
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
                    },
                },
            },
        }
    ]