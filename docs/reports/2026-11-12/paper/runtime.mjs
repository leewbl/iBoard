import path from 'node:path';import {pathToFileURL} from 'node:url';
export async function dependency(name,entry){if(process.env.IBOARD_DOCS_NODE_MODULES)return import(pathToFileURL(path.join(process.env.IBOARD_DOCS_NODE_MODULES,name,entry)).href);return import(name);}
