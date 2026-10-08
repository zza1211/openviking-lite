"""Run after ov-lite serve: python examples/memory_demo.py."""

import os

from openviking_lite import Client

with Client(api_key=os.environ.get("OV_LITE_API_KEY", "")) as client:
    client.add_text("本项目使用 Python 和 SQLite，部署时不需要 GPU。", "viking://resources/demo")
    print(client.find("是否需要显卡？"))
    print(client.ask("项目采用什么数据库？"))
    session = client.create_session("alice")["session_id"]
    client.add_message(session, "user", "以后请用简体中文回答我，代码例子优先用 Python。")
    print(client.commit_session(session))
    print(client.find("用户喜欢怎样的回答？", target_uri="viking://user/alice/memories"))
