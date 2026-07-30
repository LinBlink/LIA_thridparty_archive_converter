import argparse
import json
import os
import sys


def convert_txt_to_json(txt_path):
    # 1. 检查输入文件是否存在
    if not os.path.exists(txt_path):
        print(f"错误: 找不到输入文件 '{txt_path}'")
        sys.exit(1)

    # 2. 读取 txt 文件内容
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"读取文件时出错: {e}")
        sys.exit(1)

    # 3. 构造 JSON 数据结构
    json_data = {"content": content}

    # 4. 创建 ./data 输出目录（如果不存在的话）
    output_dir = "./data"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 5. 获取原文件名并拼接新的输出路径
    base_name = os.path.basename(txt_path)  # 获取文件名，例如 'example.txt'
    file_name_without_ext = os.path.splitext(base_name)[0]  # 获取不带后缀的名字 'example'
    output_path = os.path.join(output_dir, f"{file_name_without_ext}.json")

    # 6. 写入 JSON 文件
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            # ensure_ascii=False 保证中文不被转义为 \uXXXX，indent=4 让输出美观
            json.dump(json_data, f, ensure_ascii=False, indent=4)
        print(f"成功！JSON 文件已生成至: {output_path}")
    except Exception as e:
        print(f"写入 JSON 时出错: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # 使用 argparse 解析命令行参数
    parser = argparse.ArgumentParser(
        description="将 TXT 文本内容转换为 JSON 格式并存入 ./data 目录"
    )
    parser.add_argument(
        "input_file", help="需要转换的 txt 文件路径 (例如: test.txt 或 ../demo.txt)"
    )

    args = parser.parse_args()

    # 执行转换
    convert_txt_to_json(args.input_file)