from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from virtuoso_bridge import VirtuosoClient, decode_skill_output

client = VirtuosoClient.local(port=65432)


def add(a: int, b: int) -> int:
    """Add a and b using Virtuoso SKILL.

    Args:
        a: first int
        b: second int
    """
    result = client.execute_skill(f"plus({a} {b})")
    return int(decode_skill_output(result.output))


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
