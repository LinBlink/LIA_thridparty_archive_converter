import markdown
import json
import os
import sys

def extract_json_content_to_md( json_file_path, output_dir=r'.\2md' ):
  if not os.path.exists(output_dir):
    os.makedirs(output_dir)
  
  try:
    with open(json_file_path, 'r', encoding='utf-8') as f:
      json_data = json.load(f)
  except FileNotFoundError:
    print(f"错误：未找到文件 {json_file_path}")
    return
  except json.JSONDecodeError:
    print(f"错误：{json_file_path} 不是合法的 JSON 文件")
  else:
    if isinstance( json_data , dict) and ( 'content' in json_data ) :
      md_string = json_data['content']
    else:
      print("错误：JSON 数据中不包含 'content' 键")
      return
    
    file_name = os.path.splitext(os.path.basename(json_file_path))[0]
    output_file_path = os.path.join( output_dir, f"{file_name}.md" )

    with open( output_file_path, 'w', encoding='utf-8' ) as f:
      f.write(md_string)

    print(f"保存成功！Markdown 文件已经写入:{output_file_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
      print("用法: python polish.py <input.json>")
      sys.exit(1)
