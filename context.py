import json
import argparse
import os
import subprocess
from neo4j import GraphDatabase
from dotenv import load_dotenv


class ArgsParser:
    def __init__(self):
        self.parser = argparse.ArgumentParser(description="JS/TS Dependency Graph Tool")
        subparsers = self.parser.add_subparsers(dest="command")

        upload_parser = subparsers.add_parser("upload", help="Upload dependency graph to Neo4j")
        upload_parser.add_argument("-f", "--full-context-file", default="output/context.json", help="Path to context.json file")
        upload_parser.add_argument("-r", "--run-analyzer", action="store_true", help="Run JS analyzer to generate context.json")
        upload_parser.add_argument("-p", "--path", help="Path to source project for --run-analyzer")

        context_parser = subparsers.add_parser("get-context", help="Get dependency context for a function")
        context_parser.add_argument("-n", "--function-name", required=True, help="Function/component name")
        context_parser.add_argument("-d", "--depth", default="*:*", help="Depth as PARENTS:CHILDREN (default *:*)")
        context_parser.add_argument("-f", "--full-context-file", default="output/context.json", help="Path to context.json")
        context_parser.add_argument("-o", "--output-file", default="output/context.txt", help="Write output to this file")
        context_parser.add_argument("-c", "--include-function-code", action="store_true", help="Include function code if available")
        context_parser.add_argument("-C", "--no-files-content", action="store_true", help="Exclude file contents from output")

    def parse(self):
        return self.parser.parse_args()


def load_dependencies(json_file):
    with open(json_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def push_to_neo4j(context, uri, user, password):
    nodes = context.get("nodes", [])
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        session.execute_write(clear_graph)

        function_index = {}
        for item in nodes:
            file = item['file']
            func = item['function']
            full_func = f"{file}::{func}"
            function_index[func] = {
                'id': full_func,
                'label': func,
                'group': os.path.dirname(file),
                'file': file,
                'code': item.get('code', '')
            }

        for item in nodes:
            func_entry = function_index[item['function']]
            session.execute_write(create_node, **func_entry)

            for dep in item.get("dependencies", []):
                dep_entry = function_index.get(dep)
                if dep_entry:
                    session.execute_write(create_node, **dep_entry)
                    session.execute_write(create_edge, func_entry['id'], dep_entry['id'])
                else:
                    unresolved_id = f"unknown::{dep}"
                    session.execute_write(create_node, unresolved_id, dep, 'unknown', '', '')
                    session.execute_write(create_edge, func_entry['id'], unresolved_id)

            for ext in item.get("dependenciesExternal", []):
                session.execute_write(create_node, ext, ext, 'external', '', '')
                session.execute_write(create_edge, func_entry['id'], ext)
    driver.close()


def clear_graph(tx):
    tx.run("MATCH (n) DETACH DELETE n")


def create_node(tx, id, label, group, file, code):
    tx.run(
        "MERGE (n:Node {id: $id}) SET n.label = $label, n.group = $group, n.file = $file, n.code = $code",
        id=id, label=label, group=group, file=file, code=code
    )


def create_edge(tx, src_id, dst_id):
    tx.run(
        "MATCH (a:Node {id: $src}), (b:Node {id: $dst}) MERGE (a)-[:DEPENDS_ON]->(b)",
        src=src_id, dst=dst_id
    )


def depth2neo4j(depth_range: str, direction: str) -> str:
    try:
        up_raw, down_raw = (depth_range.split(":") + [""])[:2]
    except ValueError:
        raise ValueError("Invalid depth format. Use format like '*:*', '2:3', '*:0', etc.")

    if direction == "parent":
        if up_raw.strip() == "*" or up_raw == "":
            return "0.."
        elif up_raw.isdigit():
            return f"0..{up_raw}"
        else:
            raise ValueError("Invalid parent depth")
    elif direction == "child":
        if down_raw.strip() == "*" or down_raw == "":
            return "1.."
        elif down_raw == "0":
            return ""
        elif down_raw.isdigit():
            return f"1..{down_raw}"
        else:
            raise ValueError("Invalid child depth")
    return ""


def get_context(function_name, uri, user, password, depth, context_file, output_file, include_code, skip_files_content):
    context_data = load_dependencies(context_file)
    files_map = {entry["path"]: entry["content"] for entry in context_data.get("filesContent", [])}

    parent_range = depth2neo4j(depth, "parent")
    child_range = depth2neo4j(depth, "child")

    driver = GraphDatabase.driver(uri, auth=(user, password))

    parents = []
    children = []
    files_in_context = set()
    target_info = None

    with driver.session() as session:

        # Parent query
        parent_query = f"""
        MATCH (target:Node {{label: $name}})
        OPTIONAL MATCH path = (target)<-[:DEPENDS_ON*{parent_range}]-(p)
        WITH collect(nodes(path)) AS nodes, target
        UNWIND nodes AS n
        UNWIND n AS x
        RETURN DISTINCT x.label AS label, x.file AS file, x.code AS code, target.label AS targetLabel
        """
        parent_result = session.run(parent_query, name=function_name)
        for record in parent_result:
            label = record["label"]
            file = record["file"]
            code = record.get("code", "")
            files_in_context.add(file)
            if label == function_name:
                target_info = f"\n🎯 Component/Function of interest: {function_name}\n\tFile: {file}"
                if include_code and code:
                    target_info += f"\nCode:\n{code}"
                continue
            block = f"\n🔹 {label}\n\tFile: {file}"
            if include_code and code:
                block += f"\n\tCode:\n{code}"
            parents.append(block)

        # Child query
        if child_range:
            child_query = f"""
            MATCH (target:Node {{label: $name}})
            OPTIONAL MATCH path2 = (target)-[:DEPENDS_ON*{child_range}]->(c)
            WITH collect(nodes(path2)) AS nodes, target
            UNWIND nodes AS n
            UNWIND n AS x
            RETURN DISTINCT x.label AS label, x.file AS file, x.code AS code
            """
            child_result = session.run(child_query, name=function_name)
            for record in child_result:
                label = record["label"]
                file = record["file"]
                code = record.get("code", "")
                files_in_context.add(file)
                if label == function_name:
                    continue
                block = f"\n🔹 {label}\n\tFile: {file}"
                if include_code and code:
                    block += f"\n\tCode:\n{code}"
                children.append(block)

    out = []
    if target_info:
        out.append(target_info)
    else:
        out.append(f"\n🎯 Component/Function '{function_name}' not found in graph.")

    out.append("\n⬆️  Parent (calling) components/functions:")
    out.append("None" if not parents else "\n".join(parents))

    out.append("\n⬇️  Children (called) components/functions:")
    out.append("None" if not children else "\n".join(children))

    if not skip_files_content:
        out.append("\n📄 Included File Contents:")
        for file in sorted(files_in_context):
            content = files_map.get(file, None)
            if content:
                out.append(f"\n--- {file} ---\n{content}\n")

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    print(f"✅ Context saved to {output_file}")
    driver.close()


def main():
    load_dotenv()
    args = ArgsParser().parse()

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "test1234")

    if args.command == "upload":
        if args.run_analyzer:
            if not args.path:
                print("❌ --path is required when using --run-analyzer")
                return
            full_context_file = args.full_context_file
            print("⚙️ Running JS analyzer...")
            subprocess.run(["node", "./gengraph.js", "-p", args.path, "-o", full_context_file], check=True)
        context = load_dependencies(args.full_context_file)
        push_to_neo4j(context, uri, user, password)
        print("✅ Graph imported to Neo4j with full function metadata.")

    elif args.command == "get-context":
        depth = args.depth if ':' in args.depth else f"{args.depth}:{args.depth}"
        get_context(
            args.function_name,
            uri,
            user,
            password,
            depth,
            args.full_context_file,
            args.output_file,
            args.include_function_code,
            args.no_files_content
        )
    else:
        print("❌ Unknown command. Use --help for guidance.")


if __name__ == "__main__":
    main()
