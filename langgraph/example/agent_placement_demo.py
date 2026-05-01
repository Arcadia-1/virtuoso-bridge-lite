"""Analog layout agent — reads a schematic then places instances in a new layout.

Usage:
    python example/create_layout_agent.py
    python example/create_layout_agent.py --debug
"""

import json
import argparse
import logging
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

log = logging.getLogger(__name__)

client = VirtuosoClient.local(port=65432)


def check_layout_exists(lib: str, cell: str) -> str:
    """Check whether a layout cellview already exists for the given lib/cell.

    Args:
        lib: library name
        cell: cell name
    """
    log.debug("check_layout_exists(lib=%s, cell=%s)", lib, cell)
    result = client.execute_skill(
        f'if(dbOpenCellViewByType("{lib}" "{cell}" "layout" "maskLayout" "r") "exists" "not_found")'
    )
    output = decode_skill_output(result.output)
    log.debug("check_layout_exists → %s", output)
    return output


def delete_layout(lib: str, cell: str) -> str:
    """Delete the layout cellview for the given lib/cell.

    Args:
        lib: library name
        cell: cell name
    """
    log.debug("delete_layout(lib=%s, cell=%s)", lib, cell)
    skill = f"""
let((ddview)
    foreach(win hiGetWindowList()
        when(win~>cellView
            && win~>cellView~>libName == "{lib}"
            && win~>cellView~>cellName == "{cell}"
            && win~>cellView~>viewName == "layout"
            dbSave(win~>cellView)
            hiCloseWindow(win)))
    ddview = ddGetObj("{lib}" "{cell}" "layout")
    if(ddview then
        ddDeleteObj(ddview)
        sprintf(nil "deleted: {lib}/{cell}/layout")
    else
        sprintf(nil "not found: {lib}/{cell}/layout")))
"""
    result = client.execute_skill(skill)
    output = decode_skill_output(result.output)
    log.debug("delete_layout → %s", output)
    return output


def read_schematic_tool(lib: str, cell: str) -> str:
    """Read the schematic of a cell and return its instances, nets, and pins.

    Args:
        lib: library name
        cell: cell name
    """
    log.debug("read_schematic_tool(lib=%s, cell=%s)", lib, cell)
    data = read_schematic(client, lib, cell)
    compact = {
        "instances": [
            {"name": i["name"], "lib": i["lib"], "cell": i["cell"]}
            for i in data["instances"]
        ],
        "nets": {
            name: net["connections"]
            for name, net in data["nets"].items()
        },
        "pins": list(data["pins"].keys()),
    }
    output = json.dumps(compact, indent=2)
    log.debug("read_schematic_tool → %s", output)
    return output


def place_instance(
    target_lib: str,
    target_cell: str,
    inst_lib: str,
    inst_cell: str,
    inst_name: str,
    x: float,
    y: float,
    orientation: str = "R0",
) -> str:
    """Place a layout instance in the target cell at the given position.

    Args:
        target_lib: library of the layout cell to create/edit
        target_cell: cell name of the layout to create/edit
        inst_lib: library of the instance master cell
        inst_cell: cell name of the instance master
        inst_name: name to give the instance
        x: x coordinate
        y: y coordinate
        orientation: orientation string e.g. R0, R90, MY, MX
    """
    log.debug("place_instance(%s/%s → %s/%s '%s' @ (%s, %s) %s)",
              inst_lib, inst_cell, target_lib, target_cell, inst_name, x, y, orientation)
    skill = f"""
let((cv)
    cv = dbOpenCellViewByType("{target_lib}" "{target_cell}" "layout" "maskLayout" "a")
    dbCreateParamInstByMasterName(cv "{inst_lib}" "{inst_cell}" "layout" "{inst_name}" list({x} {y}) "{orientation}")
    dbSave(cv)
    dbClose(cv)
    "placed {inst_name} at ({x} {y})")
"""
    result = client.execute_skill(skill)
    output = decode_skill_output(result.output)
    log.debug("place_instance → %s", output)
    return output



llm = ChatOpenAI(model="gpt-4o")
tools = [check_layout_exists, delete_layout, read_schematic_tool, place_instance]
llm_with_tools = llm.bind_tools(tools)


def tool_calling_llm(state: MessagesState):
    response = llm_with_tools.invoke(state["messages"])
    if isinstance(response, AIMessage) and response.tool_calls:
        for tc in response.tool_calls:
            log.debug("[LLM] tool call: %s(%s)", tc["name"], tc["args"])
    else:
        log.debug("[LLM] final response")
    return {"messages": [response]}


builder = StateGraph(MessagesState)
builder.add_node("tool_calling_llm", tool_calling_llm)
builder.add_node("tools", ToolNode(tools))
builder.add_edge(START, "tool_calling_llm")
builder.add_conditional_edges("tool_calling_llm", tools_condition)
builder.add_edge("tools", "tool_calling_llm")
graph = builder.compile()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )

    messages = [HumanMessage(content=(
        "Create a layout for library 'test', cell 'inverter'. "
        "First check if the layout already exists and delete it if so. "
        "Then read the schematic and place each instance at a reasonable position."
    ))]
    result = graph.invoke({"messages": messages})
    result["messages"][-1].pretty_print()
