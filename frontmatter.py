from dataclasses import dataclass, field

@dataclass
class FrontmatterResult:
    """Frontmatter 解析结果，包含元数据字典和正文内容。"""
    meta: dict[str, str] = field(default_factory=dict)  # YAML 风格的键值对元数据
    body: str = ""                                        # 去除 Frontmatter 后的正文

def parse_frontmatter(content: str) -> FrontmatterResult:
    """从 Markdown 内容中解析 YAML Frontmatter 元数据块。
    格式约定：文件以 --- 开头和结尾包裹元数据，中间为 key: value 键值对。
    """
    lines = content.splitlines()
    # 第一行必须是 ---，否则认为没有 Frontmatter
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(meta={}, body=content)
    
    # 查找结束标记 ---
    end_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_index = i
            break

    # 未找到闭合标记，返回原始内容作为正文
    if end_index is None:
        return FrontmatterResult(body=content)
    
    # 逐行解析 key: value 格式的元数据
    meta: dict[str, str] = {}
    for i in range(1, end_index):
        colon = lines[i].find(":")
        if colon == -1:
            continue  # 忽略不符合 key: value 格式的行
        key = lines[i][:colon].strip()
        value = lines[i][colon + 1:].strip()
        if key:
            meta[key] = value

    # 跳过结束标记，重组正文部分
    body = "\n".join(lines[end_index + 1:]).strip()
    return FrontmatterResult(meta=meta, body=body)  

def format_frontmatter(meta: dict[str, str], body: str) -> str:
    """将元数据字典和正文内容格式化为标准的 Frontmatter 文件格式。"""
    if not meta:
        return body.strip()  # 没有元数据，直接返回正文

    # 构建 Frontmatter 块
    frontmatter_lines = ["---"]
    for key, value in meta.items():
        frontmatter_lines.append(f"{key}: {value}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")
    frontmatter_lines.append(body)
    return "\n".join(frontmatter_lines)