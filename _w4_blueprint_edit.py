p = 'Standalone tools/local-model/星澄模型四層建置藍圖.md'
content = open(p, encoding='utf-8').read()

# §10.53 item 2 status update
old2 = ('   （W4-3，2026-09-22：全欄位綁定執行模組、驗證器 18/18 PASS、12 implemented／\n'
        '   6 partially-implemented 誠實落差）；殘＝落差欄位補實與 closure evaluator 接線。')
new2 = ('   （W4-3，2026-09-22：全欄位綁定執行模組、驗證器 18/18 PASS、落差補實後\n'
        '   15 implemented／3 partially-implemented——request_registry 七欄＋stream_owner＋\n'
        '   artifact_hash 已補）；殘＝backpressure_limit／治理拓撲兩契約與 closure evaluator 接線。')
assert old2 in content, 'old2 not found'
content = content.replace(old2, new2)

# §11.2 W4 append item 4 after the W4-3 line
anchor = '拓撲與版本軸收斂另見 §10.53 項 1。'
idx = content.find(anchor)
assert idx > 0
insert = ('\n4. 落差欄位補實（W4-3 殘項）：**部分落地**——request_registry.py 新增 codex 八欄'
          '（streaming_attribution／priority_class／correlation_id／operation_id／actor_id／module_id／'
          'decision_id／stream_owner；加性、舊狀態檔相容、upsert kwargs＋legacy dict 對映全通）→ '
          'REQUEST_REGISTRY＋REQUEST_LIFECYCLE 升 implemented；chat_foundation_dataset.py 產出 sha256 '
          'sidecar＋manifest artifact_hash → PRETRAIN_DATA_PIPELINE 升 implemented；測試 41/41 PASS、'
          '驗證器 18/18 PASS（15 implemented／3 partial，證據 contract-registry-validation-20260922T182343Z.json）。'
          '殘＝connection_state backpressure_limit 無現行機制可綁、system_convergence／five_plane 治理拓撲層、'
          'closure evaluator 接線。')
content = content[:idx + len(anchor)] + insert + content[idx + len(anchor):]

open(p, 'w', encoding='utf-8').write(content)
print('blueprint updated')
