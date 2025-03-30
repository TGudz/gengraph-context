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
        context_parser.add_argument("-d", "--depth", default="0:-1", help="Depth as PARENTS:CHILDREN (default -1:0)")
        context_parser.add_argument("-F", "--output-filenames-only", action="store_true", help="Print only the filenames (no code or metadata)")

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
    start, end = map(int, depth_range.split(":"))
    if direction == "parent":
        return "0.." if start == -1 else f"0..{start}"
    elif direction == "child":
        return "1.." if end == -1 else f"1..{end}"
    return ""


def get_context(function_name, uri, user, password, depth, filenames_only=False):
    parent_range = depth2neo4j(depth, "parent")
    child_range = depth2neo4j(depth, "child")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        query = f"""
        MATCH (target:Node {{label: $name}})
        OPTIONAL MATCH path1 = (target)<-[:DEPENDS_ON*{parent_range}]-(parent)
        OPTIONAL MATCH path2 = (target)-[:DEPENDS_ON*{child_range}]->(child)
        WITH target, collect(DISTINCT parent) AS parents, collect(DISTINCT child) AS children
        RETURN target, parents, children
        """
        result = session.run(query, name=function_name)

        record = result.single()
        if not record:
            print(f"❌ No function named '{function_name}' found.")
            return

        target = record["target"]
        parents = record["parents"]
        children = record["children"]

        print(f"\n{target['label']} function:")
        print(f"  File: {target.get('file') or '<no file>'}")

        if filenames_only:
            if parents:
                print("\nParent (calling) functions:")
                for p in sorted(parents, key=lambda x: x['label']):
                    print(f"🔹 {p['label']}\n  {p.get('file') or '<no file>'}")

            if children:
                print("\nChildren (called) functions:")
                for c in sorted(children, key=lambda x: x['label']):
                    print(f"🔹 {c['label']}\n  {c.get('file') or '<no file>'}")
        else:
            print("\nParent (calling) functions:")
            if parents:
                for p in sorted(parents, key=lambda x: x['label']):
                    print(f"\n🔹 {p['label']}")
                    print(f"File: {p.get('file') or '<no file>'}")
                    print("Code:\n" + (p.get('code') or "<no code>"))
                    print("-" * 80 + "\n")
            else:
                print("  (none)")

            print("\nChildren (called) functions:")
            if children:
                for c in sorted(children, key=lambda x: x['label']):
                    print(f"\n🔹 {c['label']}")
                    print(f"File: {c.get('file') or '<no file>'}")
                    print("Code:\n" + (c.get('code') or "<no code>"))
                    print("-" * 80 + "\n")
            else:
                print("  (none)")

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
        get_context(args.function_name, uri, user, password, args.depth, filenames_only=args.output_filenames_only)

    else:
        print("❌ Unknown command. Use --help for guidance.")


if __name__ == "__main__":
    main()
