#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const parser = require('@babel/parser');
const traverse = require('@babel/traverse').default;
const { Command } = require('commander');

const program = new Command();

program
  .name('analyzejs')
  .description('Analyze JS/TS files and extract function/component dependencies.')
  .requiredOption('-p, --path <path>', 'JS/TS file or directory to analyze')
  .option('-e, --external-dependencies', 'Include external dependencies in the result')
  .option('-o, --output-file <file>', 'Write output to the specified file', 'output/context.json')
  .option('--verbosity <level>', 'Verbosity level: quiet | info | debug', 'info')
  .option('-x, --exclude <dirs...>', 'Directories to exclude from scanning', (value, prev) => prev.concat(value), [])
  .version('1.0.0')
  .parse();

const options = program.opts();
const inputPath = options.path;
const verbosity = options.verbosity;
const includeExternal = options.externalDependencies;
const outputFile = options.outputFile;
const excludeDirs = new Set(['node_modules', 'dist', 'public', ...(options.exclude || [])]);

const VERBOSE = {
  quiet: 0,
  info: 1,
  debug: 2,
}[verbosity] ?? 1;

function log(...args) {
  if (VERBOSE >= 2) console.log('[DEBUG]', ...args);
}

function isSupportedFile(filePath) {
  return filePath.endsWith('.js') || filePath.endsWith('.jsx') || filePath.endsWith('.ts') || filePath.endsWith('.tsx');
}

function findJsxFiles(dirPath) {
  const result = [];

  function walk(currentPath) {
    const stats = fs.statSync(currentPath);
    if (stats.isFile() && isSupportedFile(currentPath)) {
      result.push(currentPath);
    } else if (stats.isDirectory()) {
      const dirName = path.basename(currentPath);
      if (excludeDirs.has(dirName)) return;
      const entries = fs.readdirSync(currentPath);
      for (const entry of entries) {
        walk(path.join(currentPath, entry));
      }
    }
  }

  const fullPath = path.resolve(dirPath);
  walk(fullPath);

  return result;
}

const importSymbolToSourceMap = new Map();           // alias → source file
const fileToDeclaredSymbols = new Map();             // source file → Set of declared function names

function analyzeFile(filePath) {
  const code = fs.readFileSync(filePath, 'utf-8');
  const ast = parser.parse(code, { sourceType: 'module', plugins: ['jsx', 'typescript'] });

  const implemented = new Set();
  const importedFromNodeModules = new Set();
  const importedLocally = new Set();
  const callsPerFunction = {};
  const variablesDeclaredInFunction = {};
  const functionParents = {};
  const scopeStack = [];

  function currentScope() {
    return scopeStack[scopeStack.length - 1];
  }

  function registerCall(name) {
    const scope = currentScope();
    if (!scope) return;
    callsPerFunction[scope] ||= new Set();
    callsPerFunction[scope].add(name);
  }

  function registerVariable(name) {
    const scope = currentScope();
    if (!scope) return;
    variablesDeclaredInFunction[scope] ||= new Set();
    variablesDeclaredInFunction[scope].add(name);
  }

  function registerVariableFromPattern(patternNode) {
    if (!patternNode) return;
    if (patternNode.type === 'Identifier') {
      registerVariable(patternNode.name);
    } else if (patternNode.type === 'ArrayPattern') {
      for (const element of patternNode.elements) {
        if (element?.type === 'Identifier') {
          registerVariable(element.name);
        }
      }
    } else if (patternNode.type === 'ObjectPattern') {
      for (const prop of patternNode.properties) {
        if (prop.type === 'ObjectProperty' && prop.value.type === 'Identifier') {
          registerVariable(prop.value.name);
        } else if (prop.type === 'RestElement' && prop.argument.type === 'Identifier') {
          registerVariable(prop.argument.name);
        }
      }
    }
  }

  traverse(ast, {
    FunctionDeclaration: {
      enter(path) {
        const name = path.node.id.name;
        implemented.add(name);
        if (currentScope()) functionParents[name] = currentScope();
        scopeStack.push(name);
        log(`Function '${name}' (${functionParents[name] ? 'nested in ' + functionParents[name] : 'top-level'})`);
      },
      exit() {
        scopeStack.pop();
      },
    },

    VariableDeclarator: {
      enter(path) {
        registerVariableFromPattern(path.node.id);

        const isFunction =
          path.node.init &&
          (path.node.init.type === 'ArrowFunctionExpression' || path.node.init.type === 'FunctionExpression');

        if (isFunction) {
          const name = path.node.id.name;
          implemented.add(name);
          if (currentScope()) functionParents[name] = currentScope();
          scopeStack.push(name);
          log(`Function '${name}' (${functionParents[name] ? 'nested in ' + functionParents[name] : 'top-level'})`);
        }
      },
      exit(path) {
        const isFunction =
          path.node.init &&
          (path.node.init.type === 'ArrowFunctionExpression' || path.node.init.type === 'FunctionExpression');

        if (isFunction) scopeStack.pop();
      },
    },

    ClassDeclaration: {
      enter(path) {
        const name = path.node.id.name;
        implemented.add(name);
        if (currentScope()) functionParents[name] = currentScope();
        scopeStack.push(name);
        log(`Class '${name}' (${functionParents[name] ? 'nested in ' + functionParents[name] : 'top-level'})`);
      },
      exit() {
        scopeStack.pop();
      },
    },

    ImportDeclaration(pathNode) {
      const source = pathNode.node.source.value;
      let resolvedSourcePath = path.resolve(path.dirname(filePath), source);
      if (!fs.existsSync(resolvedSourcePath)) {
        for (const ext of ['.js', '.jsx', '.ts', '.tsx']) {
          if (fs.existsSync(resolvedSourcePath + ext)) {
            resolvedSourcePath += ext;
            break;
          }
        }
      }
      const isNodeModule = !source.startsWith('.') && !source.startsWith('/');

      for (const specifier of pathNode.node.specifiers) {
        const name = specifier.local.name;
        if (isNodeModule) {
          importedFromNodeModules.add(name);
          log(`Imported from node_modules: ${name}`);
        } else {
          importedLocally.add(name);
          importSymbolToSourceMap.set(name, resolvedSourcePath);
          log(`Imported locally: ${name} from ${resolvedSourcePath}`);
        }
      }
    },

    CallExpression(path) {
      const callee = path.node.callee;
      if (callee.type === 'Identifier') {
        registerCall(callee.name);
        log(`Call: ${callee.name} in ${currentScope()}`);
      } else if (callee.type === 'MemberExpression') {
        if (callee.object?.type === 'Identifier') {
          registerCall(callee.object.name);
          log(`Call: ${callee.object.name} in ${currentScope()}`);
        }
      }
    },

    JSXOpeningElement(path) {
      if (path.node.name.type === 'JSXIdentifier') {
        const name = path.node.name.name;
        if (name[0] === name[0].toUpperCase()) {
          registerCall(name);
          log(`JSX component: ${name} in ${currentScope()}`);
        }
      }
    },
  });

  fileToDeclaredSymbols.set(path.resolve(filePath), implemented);

  function collectAccessibleVars(fnName) {
    const vars = new Set();
    let current = fnName;
    while (current) {
      const declared = variablesDeclaredInFunction[current];
      if (declared) declared.forEach(v => vars.add(v));
      current = functionParents[current];
    }
    log(`Accessible vars for '${fnName}': ${Array.from(vars).join(', ')}`);
    return vars;
  }

  function resolveDependencies(fnName, visited = new Set()) {
    const local = new Set();
    const external = new Set();
    const accessibleVars = collectAccessibleVars(fnName);

    function walk(name) {
      if (visited.has(name)) return;
      visited.add(name);
      const calls = callsPerFunction[name];
      if (!calls) return;

      for (const callee of calls) {
        if (implemented.has(callee)) {
          local.add(callee);
          walk(callee);
        } else if (importedLocally.has(callee)) {
          const importedFrom = importSymbolToSourceMap.get(callee);
          const declared = fileToDeclaredSymbols.get(importedFrom);
          if (declared) {
            for (const real of declared) local.add(real);
          } else {
            local.add(callee);
          }
        } else if (importedFromNodeModules.has(callee)) {
          external.add(callee);
        } else if (!accessibleVars.has(callee)) {
          external.add(callee);
          log(`'${callee}' is marked as external in '${fnName}'`);
        } else {
          log(`'${callee}' is scoped/local to '${fnName}'`);
        }
      }
    }

    walk(fnName);
    local.delete(fnName);

    return {
      dependencies: Array.from(local).sort(),
      dependenciesExternal: includeExternal ? Array.from(external).sort() : undefined,
    };
  }

  const result = [];
  implemented.forEach(fn => {
    const deps = resolveDependencies(fn);
    let rawCode = '/* implementation not found */';

    traverse(ast, {
      enter(path) {
        const node = path.node;
        let name = null;

        if (path.isFunctionDeclaration() && node.id?.name === fn) name = node.id.name;
        else if (
          path.isVariableDeclarator() &&
          node.id?.name === fn &&
          (node.init?.type === 'ArrowFunctionExpression' || node.init?.type === 'FunctionExpression')
        ) name = node.id.name;
        else if (path.isClassDeclaration() && node.id?.name === fn) name = node.id.name;

        if (name === fn) {
          rawCode = code.slice(node.start, node.end);
          path.stop();
        }
      }
    });

    const output = {
      file: path.resolve(filePath),
      function: fn,
      dependencies: deps.dependencies,
      code: rawCode,
      fileContent: code
    };

    if (includeExternal && deps.dependenciesExternal?.length) {
      output.dependenciesExternal = deps.dependenciesExternal;
    }

    result.push(output);
  });

  return result;
}

const allFiles = findJsxFiles(inputPath);
let finalResults = [];

for (const file of allFiles) {
  log(`Analyzing ${file}`);
  try {
    const res = analyzeFile(file);
    finalResults.push(...res);
  } catch (err) {
    console.error(`Error analyzing ${file}:`, err.message);
  }
}

const resolvedOutputPath = path.resolve(outputFile);
fs.mkdirSync(path.dirname(resolvedOutputPath), { recursive: true });
fs.writeFileSync(resolvedOutputPath, JSON.stringify(finalResults, null, 2), 'utf-8');

if (VERBOSE >= 1) {
  console.log(`Output written to ${resolvedOutputPath}`);
}
