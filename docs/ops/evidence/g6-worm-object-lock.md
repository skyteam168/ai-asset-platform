# G6.4 WORM 应用层校验（本地）

- endpoint: `http://127.0.0.1:58333`
- bucket: `aip-audit-worm`
- key: `drill/g64-8051f3b83e444380874e80158c9ab276.json`
- 结果：同内容幂等写入成功；冲突内容拒绝；读回一致
- 说明：此为应用层 put_if_absent；**合规 Object Lock 桶**仍须 staging 运维勾选
- 命令: `.venv/Scripts/python tools/worm_object_lock_check.py`
