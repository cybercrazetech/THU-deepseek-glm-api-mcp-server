import openai
# 设置 OpenAI API 密钥
openai.api_key = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2RlIjoiMTAyNyIsImlhdCI6MTc3Mzg5Njk4MywiZXhwIjoxNzczOTE4NTgzfQ.v2kUPUUDyayZakBeNo3cRGrejTFCTjm37bKmRuFPqig'
openai.api_base = 'https://madmodel.cs.tsinghua.edu.cn/v1/'
# 调用 Chat Completion API
response = openai.ChatCompletion.create(
model="DeepSeek-R1-671B",
messages=[
{"role": "user", "content": "你好"}
],
temperature=0.6,
repetition_penalty = 1.2,
stream = False
)
print(response.choices[0].message['content'])
