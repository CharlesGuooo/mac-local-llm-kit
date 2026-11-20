# mac-local-llm-kit

给 MacBook Pro 16 英寸(M3 Pro · 36 GB 统一内存 · macOS 26 Tahoe)离线部署本地大模型用的包:两个推理引擎、八份模型权重、合卷脚本。

- 大文件都在 [Releases](https://github.com/CharlesGuooo/mac-local-llm-kit/releases)。超过 1 GiB 的文件按 1 GiB 切成 `<文件名>.part001`、`.part002`……,按顺序拼回去就和原文件逐字节相同。
- `mac/fetch.sh` 负责下载、逐卷校验、合卷,再用 Hugging Face 官方公布的 SHA-256 校验整个文件。
- Mac 只需要能访问 `github.com`、它跳转到的 `release-assets.githubusercontent.com`,以及取脚本用的 `raw.githubusercontent.com`。不需要 Hugging Face、Homebrew 或 Python。

## 给 Mac 上的 agent:按顺序做

如果这是公司电脑,装引擎和下载权重之前先按公司规定拿到批准。

### 1. 取脚本

```sh
mkdir -p ~/llm-kit/kit/mac
curl -fL -o ~/llm-kit/kit/mac/fetch.sh https://raw.githubusercontent.com/CharlesGuooo/mac-local-llm-kit/main/mac/fetch.sh
```

装了 git 的话也可以 `git clone https://github.com/CharlesGuooo/mac-local-llm-kit.git ~/llm-kit/kit`(没装 Xcode 命令行工具时 `git` 会弹安装框,用上面的 curl 就行)。

### 2. 自检,几秒钟

```sh
sh ~/llm-kit/kit/mac/fetch.sh selftest ~/llm-kit/selftest && rm -rf ~/llm-kit/selftest
```

最后一行是 `== done: selftest is complete in ...` 就说明下载、校验、合卷都能用。

### 3. 下载并合卷

命令格式是 `sh fetch.sh <tag> <目标目录>`。中断了就重跑同一条命令:已经校验过的会跳过,没下完的接着下。下载时间长的话,前面加 `caffeinate -i`,防止 Mac 睡眠。

| tag | 内容 | 大小 | 目标目录 | 用途 |
|---|---|---|---|---|
| `engines` | llama.cpp b12110(= v0.9.0)macOS arm64 包;oMLX 0.7.3 DMG(macOS 26 版) | 0.84 GiB | `~/llm-kit/engines` | 推理引擎 |
| `cfg1-qwen3.8-27b-q5_k_s-gguf` | Qwen3.8-27B Q5_K_S GGUF(bartowski) | 18.22 GiB | `~/models/cfg1-qwen3.8-27b-q5_k_s-gguf` | 配置 1 最聪明可跑,llama.cpp |
| `cfg2-qwen3.8-27b-oq4e-mtp-mlx` | Qwen3.8-27B oQ4e-mtp MLX(Jundot,2026-11-19 重新量化版,见第 5 步说明) | 16.34 GiB | `~/.omlx/models/Qwen3.8-27B-oQ4e-mtp` | 配置 2 日常主力,oMLX |
| `cfg3-qwen3.6-35b-a3b-4bit-mlx` | Qwen3.6-35B-A3B 4-bit MLX(mlx-community) | 19.03 GiB | `~/.omlx/models/Qwen3.6-35B-A3B-4bit` | 配置 3 快速辅助,oMLX |
| `alt-qwen3.8-27b-q4_k_m-gguf` | Qwen3.8-27B Q4_K_M GGUF(bartowski) | 16.24 GiB | `~/models/alt-qwen3.8-27b-q4_k_m-gguf` | 配置 1 的退路、128K 长文档、配置 2 的 llama.cpp 备选 |
| `alt-qwen3.8-27b-ud-q6_k-gguf` | Qwen3.8-27B UD-Q6_K GGUF(unsloth) | 20.47 GiB | `~/models/alt-qwen3.8-27b-ud-q6_k-gguf` | 上限文件第一步,长思考测试没过才用 |
| `alt-qwen3.8-27b-q6_k-gguf` | Qwen3.8-27B Q6_K GGUF(bartowski) | 22.22 GiB | `~/models/alt-qwen3.8-27b-q6_k-gguf` | 上限文件最后一步,同芯片有内核 panic 报告 |
| `alt-qwen3.8-27b-4bit-mlx-vision` | Qwen3.8-27B 4-bit MLX(mlx-community) | 14.97 GiB | `~/.omlx/models/Qwen3.8-27B-4bit` | 视觉与 OCR,oMLX |
| `alt-nanbeige4.3-33b-q4_k_m-gguf` | Nanbeige4.3-33B Q4_K_M GGUF(bartowski) | 19.36 GiB | `~/models/alt-nanbeige4.3-33b-q4_k_m-gguf` | 编程和终端任务备选,llama.cpp |

```sh
F=~/llm-kit/kit/mac/fetch.sh
sh $F engines                         ~/llm-kit/engines
sh $F cfg1-qwen3.8-27b-q5_k_s-gguf    ~/models/cfg1-qwen3.8-27b-q5_k_s-gguf
sh $F cfg2-qwen3.8-27b-oq4e-mtp-mlx   ~/.omlx/models/Qwen3.8-27B-oQ4e-mtp
sh $F cfg3-qwen3.6-35b-a3b-4bit-mlx   ~/.omlx/models/Qwen3.6-35B-A3B-4bit
sh $F alt-qwen3.8-27b-q4_k_m-gguf     ~/models/alt-qwen3.8-27b-q4_k_m-gguf
sh $F alt-qwen3.8-27b-ud-q6_k-gguf    ~/models/alt-qwen3.8-27b-ud-q6_k-gguf
sh $F alt-qwen3.8-27b-q6_k-gguf       ~/models/alt-qwen3.8-27b-q6_k-gguf
sh $F alt-qwen3.8-27b-4bit-mlx-vision ~/.omlx/models/Qwen3.8-27B-4bit
sh $F alt-nanbeige4.3-33b-q4_k_m-gguf ~/models/alt-nanbeige4.3-33b-q4_k_m-gguf
```

- 磁盘:脚本先检查空间,每个 release 要「它的大小 + 2 GiB」空闲。合卷时边拼边删分卷,不会占两份。九个全下约 148 GiB(159 GB),先用 `df -h ~` 看一下。
- MLX 的三个目录名就是 oMLX 里的模型 ID,下面的设置命令要用到,不要改名。
- GGUF 每个 release 放在单独的目录里(里面还有模型卡 `README.md` 和 `LICENSE`),不要放进 `~/.omlx/models`,否则 oMLX 会把 GGUF 也列成模型。

### 4. 分卷怎么合起来(想手动做时)

`fetch.sh` 做的事情,手动也能做。先把一个 release 的全部附件下到同一个目录,`MANIFEST.tsv` 也要下载。这个文件里有两种行,用制表符分隔:

```
asset  <附件名>  <字节数>  <sha256>            # 要下载的每个附件
file   <文件名>  <字节数>  <sha256>  <卷数>     # 合出来的每个文件;卷数 0 表示附件本身就是整文件
```

```sh
cd <下载目录>
awk -F'\t' '$1=="asset" {print $4"  "$2}' MANIFEST.tsv | shasum -a 256 -c -   # 1. 每一卷都要 OK
cat Qwen3.8-27B-Q5_K_S.gguf.part* > Qwen3.8-27B-Q5_K_S.gguf                   # 2. 卷号补了零,通配符的顺序就是正确顺序
rm Qwen3.8-27B-Q5_K_S.gguf.part*
shasum -a 256 -c SHA256SUMS                                                    # 3. 每个整文件都要 OK
```

`SHA256SUMS` 里的值就是 Hugging Face 公布的 SHA-256,对上了就说明和上游文件逐字节相同。仓库的 `manifests/<tag>/` 里有每个 release 的 `MANIFEST.tsv` 和 `SHA256SUMS` 副本。

### 5. 装引擎

llama.cpp(解压就能用,`llama-server` 和它的动态库在同一个目录):

```sh
tar -xzf ~/llm-kit/engines/llama-b12110-bin-macos-arm64.tar.gz -C ~/llm-kit
~/llm-kit/llama-b12110/llama-server --version
```

如果 macOS 拦着不让运行(通常只在用浏览器下载时出现):`xattr -dr com.apple.quarantine ~/llm-kit/llama-b12110`。

oMLX:

```sh
MNT=$(hdiutil attach -nobrowse -noautoopen ~/llm-kit/engines/oMLX-0.7.3-macos26-27.dmg | awk -F'\t' '/\/Volumes\// {print $NF; exit}')
ls "$MNT"                       # 看 .app 叫什么
cp -R "$MNT"/*.app /Applications/
hdiutil detach "$MNT"
```

第一次打开 oMLX 会进欢迎向导:模型目录保持默认的 `~/.omlx/models`;向导会强制设置 API key;"下载第一个模型"这一步要连 Hugging Face,能跳过就跳过,模型用上面的 `fetch.sh` 放进去。模型放好后重启 oMLX 服务,它会自动发现子目录里的模型。App 提示自动更新时先别升:报告里的设置是按 0.7.3 写的。

配置 2 的权重是作者 2026-11-19 用 oMLX 0.8.0 重新量化的版本,报告实测的 10-29 版已经从 Hugging Face 删除。两版格式相同(affine、group 64、4/5/6/8 bit 混合,`config.json` 结构一致),区别只在量化取整;按 oMLX 源码看 0.7.3 能加载,但还没在 Mac 上实测过。如果 0.7.3 加载配置 2 报错,改用 [oMLX 0.8.0](https://github.com/jundot/omlx/releases/tag/v0.8.0) 的 `oMLX-0.8.0-macos26-27.dmg`(报告没测过 0.8.0)。

### 6. 启动

**配置 1:最聪明可跑**(llama.cpp,Q5_K_S,Xhigh,64K,KV 用 q8_0)。先保存所有工作,关掉浏览器和其他大应用,接上电源:GPU 内存出问题导致的内核 panic 会让整机立刻重启。

```sh
caffeinate -i ~/llm-kit/llama-b12110/llama-server \
  -m ~/models/cfg1-qwen3.8-27b-q5_k_s-gguf/Qwen3.8-27B-Q5_K_S.gguf \
  -c 65536 -np 1 -ngl all --no-mmproj \
  -ctk q8_0 -ctv q8_0 --ctx-checkpoints 4 --cache-ram 0 \
  --reasoning-effort xhigh --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
  --port 8080
```

这条命令和报告原文一样,只是把 `-hf/-hff`(从 Hugging Face 下载)换成了 `-m` 本地路径。只有长思考测试没过,才按顺序把 `-m` 换成 UD-Q6_K、再换 Q6_K;不要一开始就跑 Q6_K。出现 `Compute error` 就退回上一级。

**配置 2 的 llama.cpp 备选**(Q4_K_M,内嵌 MTP 层,问答开 Low):

```sh
~/llm-kit/llama-b12110/llama-server \
  -m ~/models/alt-qwen3.8-27b-q4_k_m-gguf/Qwen3.8-27B-Q4_K_M.gguf \
  -c 65536 -np 1 -ngl all --no-mmproj -ctk q8_0 -ctv q8_0 \
  --spec-type draft-mtp --spec-draft-n-max 3 --ctx-checkpoints 4 --cache-ram 0 \
  --reasoning-effort low --port 8080
```

**配置 2 和配置 3**(oMLX)。下面一段是报告里的命令。原文先 `brew install jq`,这里改成先用系统自带的 `jq`,没有就用 `plutil`:

```sh
key() { jq -r .auth.api_key ~/.omlx/settings.json 2>/dev/null || plutil -extract auth.api_key raw -o - ~/.omlx/settings.json; }
export OMLX_API_KEY=$(key)            # 向导设置的 key
export PATH="$HOME/.omlx/bin:$PATH"   # 向导没帮你改 PATH 时需要
omlx stop                             # 先停掉 App 启动的服务,否则 8000 端口冲突
omlx serve --max-concurrent-requests 1 --log-level debug   # 前台运行,占住这个窗口
```

另开一个终端窗口,把上面的 `key` 函数和 `export OMLX_API_KEY=$(key)` 再执行一遍,然后:

```sh
# 不要加 --memory-guard safe。启动后看实际的内存上限(返回 JSON 里的 system.memory_guard_preview):
curl -s -H "Authorization: Bearer $OMLX_API_KEY" http://127.0.0.1:8000/admin/api/global-settings
# 模型 ID 以这条的结果为准
curl -s -H "Authorization: Bearer $OMLX_API_KEY" localhost:8000/v1/models

# 配置 2:64K,关 KV 压缩;问答用 Low
curl -X PUT "http://127.0.0.1:8000/admin/api/models/Qwen3.8-27B-oQ4e-mtp/settings" \
  -H "Authorization: Bearer $OMLX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"max_context_window":65536,"turboquant_kv_enabled":false,"chat_template_kwargs":{"reasoning_effort":"low"}}'

# 配置 3:32K,保留思考历史
curl -X PUT "http://127.0.0.1:8000/admin/api/models/Qwen3.6-35B-A3B-4bit/settings" \
  -H "Authorization: Bearer $OMLX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"max_context_window":32768,"turboquant_kv_enabled":false,"preserve_thinking":true}'
```

配置 2 的 Agent 会话:在管理面板存一个 Xhigh profile,设为 `{"chat_template_kwargs":{"reasoning_effort":"xhigh"},"max_tokens":40000}`,会话开始后不要再改。`max_tokens` 不能省,否则会被 oMLX 默认的 32,768 截断。

当 worker 用(32K、Low、全小写别名;不要和上面配置 2 的 64K 设置混用):

```sh
U=http://127.0.0.1:8000/admin/api/models; A="Authorization: Bearer $OMLX_API_KEY"; J="Content-Type: application/json"
curl -X PUT "$U/Qwen3.8-27B-oQ4e-mtp/settings" -H "$A" -H "$J" -d '{"max_context_window":32768,"turboquant_kv_enabled":false,"model_alias":"qwen38","chat_template_kwargs":{"reasoning_effort":"low"},"forced_ct_kwargs":["reasoning_effort"]}'
curl -X PUT "$U/Qwen3.6-35B-A3B-4bit/settings" -H "$A" -H "$J" -d '{"max_context_window":32768,"turboquant_kv_enabled":false,"model_alias":"qwen36","preserve_thinking":true}'
```

配置 2 和配置 3 不能同时常驻内存(两者权重加起来超过 26.4 GiB 硬线),切换来回约 15 秒。

### 7. 测试和 worker 脚本(`tools/`)

这些脚本来自同一份研究报告,报告本身不在这个仓库里。用法写在每个脚本开头,运行需要 `python3`(装 Xcode 命令行工具或 python.org 安装包后才有)。

| 脚本 | 做什么 |
|---|---|
| `lbench.py` | 对 llama.cpp 或 oMLX 的 OpenAI 接口测 decode、prefill、多轮重算,配合报告 §9 的实测清单 |
| `copilot_tap.py` | 夹在 VS Code 和本地模型服务器之间的日志代理,只记大小和哈希,不记内容 |
| `worker.py` | `run_local_task`:在一次性的 git worktree 里让本地模型干一件有边界的活,跑 gate,只回很短的回执 |
| `gate.py`、`gen_log.py` | worker 校准用的检查命令和日志样本生成器 |

## 来源和许可证

Release 里的文件都没有改动过,只是为了传输切成了分卷;合卷后和上游逐字节相同,SHA-256 记录在每个 release 的 `MANIFEST.tsv` 里。本仓库和这些模型、引擎的作者没有关系。

| 组件 | 上游(钉死的版本) | 许可证 |
|---|---|---|
| llama.cpp b12110 | [ggml-org/llama.cpp b12110](https://github.com/ggml-org/llama.cpp/releases/tag/b12110)(v0.9.0 同一提交) | MIT,`licenses/llama.cpp-MIT.txt` |
| oMLX 0.7.3 | [jundot/omlx v0.7.3](https://github.com/jundot/omlx/releases/tag/v0.7.3) | Apache-2.0,`licenses/oMLX-Apache-2.0.txt` |
| Qwen3.8-27B 的 GGUF 和 MLX 量化 | [bartowski](https://huggingface.co/bartowski/Qwen3.8-27B-GGUF)、[unsloth](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF)、[Jundot](https://huggingface.co/Jundot/Qwen3.8-27B-oQ4e-mtp)、[mlx-community](https://huggingface.co/mlx-community/Qwen3.8-27B-4bit),底座 [Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) | Apache-2.0,`licenses/Qwen3.8-27B-LICENSE.txt` |
| Qwen3.6-35B-A3B 4-bit | [mlx-community](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-4bit),底座 [Qwen/Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) | Apache-2.0,`licenses/Qwen3.6-35B-A3B-LICENSE.txt` |
| Nanbeige4.3-33B Q4_K_M | [bartowski](https://huggingface.co/bartowski/Nanbeige_Nanbeige4.3-33B-GGUF),底座 [Nanbeige/Nanbeige4.3-33B](https://huggingface.co/Nanbeige/Nanbeige4.3-33B) | Apache-2.0(模型卡声明;上游没有 LICENSE 文件),`licenses/Apache-2.0.txt` |

每个 Hugging Face 仓库钉在哪个提交,写在对应 release 的说明和 `MANIFEST.tsv` 的 `# source:` 行里。
