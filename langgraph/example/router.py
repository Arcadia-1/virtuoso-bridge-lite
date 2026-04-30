import socket
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition


def run_skill(skill_code: str, host: str = "127.0.0.1", port: int = 12345) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(skill_code.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    if not data:
        raise RuntimeError("No response from Virtuoso daemon")
    prefix = data[0]
    payload = data[1:].decode("utf-8", errors="replace")
    if prefix == 0x15:
        raise RuntimeError(f"SKILL error: {payload}")
    return payload


def add(a: int, b: int) -> int:
    """Add a and b using Virtuoso SKILL.

    Args:
        a: first int
        b: second int
    """
    result = run_skill(f"plus({a} {b})")
    return int(result)


llm = ChatOpenAI(model="gpt-4o")
llm_with_tools = llm.bind_tools([add])


def tool_calling_llm(state: MessagesState):
    return {"messages": [llm_with_tools.invoke(state["messages"])]}


builder = StateGraph(MessagesState)
builder.add_node("tool_calling_llm", tool_calling_llm)
builder.add_node("tools", ToolNode([add]))
builder.add_edge(START, "tool_calling_llm")
builder.add_conditional_edges("tool_calling_llm", tools_condition)
builder.add_edge("tools", END)
graph = builder.compile()


if __name__ == "__main__":
    messages = [HumanMessage(content="Hello, what is 2 plus 3?")]
    result = graph.invoke({"messages": messages})
    for m in result["messages"]:
        m.pretty_print()
