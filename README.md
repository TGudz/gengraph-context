# 🔍 JS/TS Function Dependency Context Extractor

This tool provides full dependency context (callers and callees) for a given JavaScript, TypeScript, JSX, or TSX function/component. It's designed to **generate rich context for LLMs** (like GPT) to answer questions about specific functions in large codebases.

---

## 🚀 What It Does

- Parses a codebase and builds a complete graph of local + external function/component dependencies.
- Pushes the graph to a Neo4j database.
- Allows querying the **context** (parent + child relationships) of any function.
- Outputs either code, files, or structure — ideal for passing to an LLM.

---

## 🧠 Why Use It?

When using an LLM to understand or modify a function, it's critical to include not just the function itself, but its **callers and callees**. This tool makes it easy to extract that context and pass it as input to AI tools.

---

## 🛠 Project Structure

- `gengraph.js`: Node.js script that statically analyzes your codebase and outputs a JSON dependency graph.
- `context.py`: Python CLI to:
  - Upload the graph to Neo4j
  - Query the context of a specific function
- `.env`: Stores connection config for Neo4j (URI, username, password)

---

## ⚙️ Installation

```bash
# Node dependencies
npm install

# Python dependencies
pip install -r requirements.txt


# Neo4j connection (create .env)
echo "NEO4J_URI=bolt://localhost:7687" > .env
echo "NEO4J_USER=neo4j" >> .env
echo "NEO4J_PASSWORD=test1234" >> .env
```


## ⚙️ Launch Neo4j DB

```bash
docker run --name neo4j-graph \
  -p 7474:7474 -p 7687:7687 \
  -d \
  -e NEO4J_AUTH=neo4j/test1234 \
  neo4j:5
```

🧪 Full Command Examples
1. Upload full dependency graph to Neo4j

python json2neo4j.py upload -r -p ../dowaw/

✔️ This will:

    Analyze the codebase at ../dowaw/

    Generate output/context.json

    Upload the results to Neo4j

2. Query a specific function’s context (parents + children)

python json2neo4j.py get-context -n SubmissionsList

✔️ This will print:

    The function itself

    Functions that call it ("parents")

    Functions it calls ("children")

    Full source code and file path

3. Output only filenames (no source code)

python json2neo4j.py get-context -n SubmissionsList -F

✔️ Use this when you only want a list of involved files (e.g. for feeding into LLM context).
4. Control the depth of parent/child traversal

python json2neo4j.py get-context -n SubmissionsList -d 2:1

✔️ This will print:

    2 levels of parent (caller) functions

    1 level of children (called functions)

python json2neo4j.py get-context -n SubmissionsList -d -1:0

✔️ This will print:

    All callers (recursively)

    No children