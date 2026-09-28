"""Redis + Chroma + LangChain + LangGraph 连通性验证"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

print("=== Redis 验证 ===")
try:
    import redis
    r = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    print(f"  PING: {r.ping()}")
    r.setex("oraclemind:test", 10, "ok")
    val = r.get("oraclemind:test")
    print(f"  SET+GET: {val}")
    print("  Redis: OK")
except Exception as e:
    print(f"  Redis: FAIL - {e}")

print()
print("=== Chroma 验证 ===")
try:
    from src.services.retrieval import get_collection, retrieve
    coll = get_collection()
    count = coll.count()
    print(f"  Collection: oraclemind_kb")
    print(f"  Documents: {count}")
    results = retrieve("戊子日柱", top_k=3)
    print(f"  Search '戊子日柱': {len(results)} results")
    for item in results[:2]:
        src = item.get("source", "")
        text = item.get("text", "")[:60]
        print(f"    [{src}] {text}...")
    print("  Chroma: OK")
except Exception as e:
    print(f"  Chroma: FAIL - {e}")

print()
print("=== LangChain 验证 ===")
try:
    from langchain_openai import ChatOpenAI
    from src.config import config
    llm = ChatOpenAI(
        model=config.ai_model_chat,
        api_key=config.ai_api_key,
        base_url=config.ai_base_url,
    )
    resp = llm.invoke("请说OK")
    print(f"  LLM model: {config.ai_model_chat}")
    print(f"  Response: {resp.content[:50]}")
    print("  LangChain: OK")
except Exception as e:
    print(f"  LangChain: FAIL - {e}")

print()
print("=== LangGraph / Agent 验证 ===")
try:
    from langchain.agents import create_agent
    print("  create_agent: imported OK")
    print("  LangGraph: OK")
except Exception as e:
    print(f"  LangGraph: FAIL - {e}")
