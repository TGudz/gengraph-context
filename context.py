import json
import argparse
import os
import subprocess
from neo4j import GraphDatabase
from dotenv import load_dotenv


class ArgsParser:
    def __init__(self):
        self.parser = argparse.ArgumentParser(description="JSX Dependency Graph Tool")
        subparsers = self.parser.add_subparsers(dest="command")

        upload_parser = subparsers.add_parser("upload", help="Upload dependency graph to Neo4j")
        upload_parser.add_argument("-f", "--full-context-file", default="output/context.json", help="Path to deps.json file")
        upload_parser.add_argument("-r", "--run-analyzer", action="store_true", help="Run JS analyzer to generate deps.json")
        upload_parser.add_argument("-p", "--path", help="Path to JSX project (used with --run-analyzer)")

        context_parser = subparsers.add_parser("get-context", help="Get dependency context for a function")
        context_parser.add_argument("-n", "--function-name", required=True, help="Function/component name")
        context_parser.add_argument("-d", "--depth", default="-1:-1", help="Depth as PARENTS:CHILDREN (default -1:0)")
        
        output_group = context_parser.add_mutually_exclusive_group()
        output_group.add_argument("-F", "--output-filenames-only", action="store_true", help="Print only the filenames (no code or metadata)")
        output_group.add_argument("-c", "--file-content", action="store_true", help="Print full file content for each function")
        
        context_parser.add_argument("-o", "--output-file", default="output/context.txt", help="Output file path (default: output/context.txt)")

    def parse(self):
        return self.parser.parse_args()


def load_dependencies(json_file):
    with open(json_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def push_to_neo4j(data, uri, user, password):
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        session.execute_write(clear_graph)

        function_index = {}
        for item in data:
            file = item['file']
            func = item['function']
            full_func = f"{file}::{func}"
            function_index[func] = {
                'id': full_func,
                'label': func,
                'group': os.path.dirname(file),
                'file': file,
                'code': item.get('code', ''),
                'fileContent': item.get('fileContent', '')
            }

        for item in data:
            func_entry = function_index[item['function']]
            session.execute_write(create_node, **func_entry)

            for dep in item.get("dependencies", []):
                dep_entry = function_index.get(dep)
                if dep_entry:
                    session.execute_write(create_node, **dep_entry)
                    session.execute_write(create_edge, func_entry['id'], dep_entry['id'])
                else:
                    unresolved_id = f"unknown::{dep}"
                    session.execute_write(create_node, unresolved_id, dep, 'unknown', '', '', '')
                    session.execute_write(create_edge, func_entry['id'], unresolved_id)

            for ext in item.get("dependenciesExternal", []):
                session.execute_write(create_node, ext, ext, 'external', '', '', '')
                session.execute_write(create_edge, func_entry['id'], ext)

    driver.close()


def clear_graph(tx):
    tx.run("MATCH (n) DETACH DELETE n")


def create_node(tx, id, label, group, file, code, fileContent):
    tx.run(
        """
        MERGE (n:Node {id: $id})
        SET n.label = $label,
            n.group = $group,
            n.file = $file,
            n.code = $code,
            n.fileContent = $fileContent
        """,
        id=id, label=label, group=group, file=file, code=code, fileContent=fileContent
    )


def create_edge(tx, src_id, dst_id):
    tx.run(
        "MATCH (a:Node {id: $src}), (b:Node {id: $dst}) MERGE (a)-[:DEPENDS_ON]->(b)",
        src=src_id, dst=dst_id
    )


def depth2neo4j(depth_range: str, direction: str) -> str:
    try:
        start, end = map(int, depth_range.split(":"))
        if direction == "parent":
            return "0.." if start == -1 else f"0..{start}"
        elif direction == "child":
            return "1.." if end == -1 else f"1..{end}"
        return ""
    except ValueError:
        print("❌ Error: Invalid depth format. Please use the format 'PARENTS:CHILDREN' (e.g. '-1:-1' or '2:1')")
        print("   Note: When using negative numbers, wrap the entire argument in quotes:")
        print("   Example: -d \"-1:-1\"")
        exit(1)


def get_context(function_name, uri, user, password, depth, filenames_only=False, file_content=False, output_file=None):
    parent_range = depth2neo4j(depth, "parent")
    child_range = depth2neo4j(depth, "child")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        query = f"""
        MATCH (target:Node {{label: $name}})
        OPTIONAL MATCH path1 = (target)<-[:DEPENDS_ON*{parent_range}]-(parent)
        OPTIONAL MATCH path2 = (target)-[:DEPENDS_ON*{child_range}]->(child)
        WITH target, 
             CASE WHEN parent IS NULL THEN [] ELSE [parent] END AS parent_list,
             CASE WHEN child IS NULL THEN [] ELSE [child] END AS child_list
        WITH target,
             [x IN parent_list WHERE x IS NOT NULL | x] AS parents,
             [x IN child_list WHERE x IS NOT NULL | x] AS children
        RETURN target, parents, children
        """
        result = session.run(query, name=function_name)

        records = list(result)
        if not records:
            message = f"❌ No function named '{function_name}' found."
            if output_file:
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(message)
            else:
                print(message)
            return

        output = []
        for i, record in enumerate(records):
            target = record["target"]
            parents = record["parents"]
            children = record["children"]

            if i > 0:
                output.append("\n" + "=" * 80 + "\n")  # Separator between multiple functions

            output.append(f"\n🔹 {target['label']} function:")
            output.append(f"File: {target.get('file') or '<no file>'}")
            if file_content:
                output.append("File Content:\n" + (target.get('fileContent') or "<no content>"))
            else:
                output.append("Code:\n" + (target.get('code') or "<no code>"))
            output.append("-" * 80 + "\n")
            if filenames_only:
                if parents:
                    output.append("\nParent (calling) functions:")
                    for p in sorted(parents, key=lambda x: x['label']):
                        output.append(f"🔹 {p['label']}\n  {p.get('file') or '<no file>'}")

                if children:
                    output.append("\nChildren (called) functions:")
                    for c in sorted(children, key=lambda x: x['label']):
                        output.append(f"🔹 {c['label']}\n  {c.get('file') or '<no file>'}")
            else:
                output.append("\nParent (calling) functions:")
                if parents:
                    for p in sorted(parents, key=lambda x: x['label']):
                        output.append(f"\n🔹 {p['label']}")
                        output.append(f"File: {p.get('file') or '<no file>'}")
                        if file_content:
                            output.append("File Content:\n" + (p.get('fileContent') or "<no content>"))
                        else: 
                            output.append("Code:\n" + (p.get('code') or "<no code>"))
                        output.append("-" * 80 + "\n")
                else:
                    output.append("  (none)")

                output.append("\nChildren (called) functions:")
                if children:
                    for c in sorted(children, key=lambda x: x['label']):
                        output.append(f"\n🔹 {c['label']}")
                        output.append(f"File: {c.get('file') or '<no file>'}")
                        if file_content:
                            output.append("File Content:\n" + (c.get('fileContent') or "<no content>"))
                        else:
                            output.append("Code:\n" + (c.get('code') or "<no code>"))
                        output.append("-" * 80 + "\n")
                else:
                    output.append("  (none)")

        output_text = "\n".join(output)
        if output_file:
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output_text)
        else:
            print(output_text)

    driver.close()


def main():
    load_dotenv()

    args = ArgsParser().parse()
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "test1234")

    if args.command == "upload":
        full_context_file = args.full_context_file
        if args.run_analyzer:
            if not args.path:
                print("❌ --path is required when using --run-js-analyzer")
                return
            print("⚙️ Running JS analyzer...")
            subprocess.run(["node", "./gengraph.js", "-p", args.path, "-o", full_context_file], check=True)

        data = load_dependencies(full_context_file)
        push_to_neo4j(data, uri, user, password)
        print("✅ Graph imported to Neo4j with full function metadata.")

    elif args.command == "get-context":
        try:
            get_context(args.function_name, uri, user, password, args.depth, 
                       filenames_only=args.output_filenames_only,
                       file_content=args.file_content,
                       output_file=args.output_file)
        except ValueError as e:
            if "depth" in str(e).lower():
                print("❌ Error: Invalid depth format. Please use the format 'PARENTS:CHILDREN' (e.g. '-1:-1' or '2:1')")
                print("   Note: When using negative numbers, wrap the entire argument in quotes:")
                print("   Example: -d \"-1:-1\"")
            else:
                print(f"❌ Error: {str(e)}")

    else:
        print("❌ Unknown command. Use --help for guidance.")


if __name__ == "__main__":
    main()
