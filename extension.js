const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');

async function activate(context) {
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    vscode.window.showErrorMessage('Codex Compact Model currently supports local VS Code on Windows x64.');
    return;
  }
  const home = process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
  const root = path.join(context.globalStorageUri.fsPath, `runtime-${context.extension.packageJSON.version}`);
  fs.mkdirSync(home, {recursive: true});
  if (!fs.existsSync(path.join(root, 'ready.json'))) {
    fs.mkdirSync(root, {recursive: true});
    fs.cpSync(path.join(context.extensionPath, 'runtime'), root, {recursive: true});
    fs.writeFileSync(path.join(root, 'launcher.json'), JSON.stringify({python: path.join(root, 'python', 'python.exe')}));
    fs.writeFileSync(path.join(root, 'ready.json'), '{}');
  }
  const official = vscode.extensions.getExtension('openai.chatgpt');
  if (official) fs.writeFileSync(path.join(root, 'official-extension.json'), JSON.stringify({extensions_dir: path.dirname(official.extensionPath)}));
  const config = path.join(home, 'compaction-routing.json');
  if (!fs.existsSync(config)) fs.writeFileSync(config, JSON.stringify({model: 'gpt-5.6-terra', reasoning_effort: 'high'}, null, 2) + '\n');
  const launcher = path.join(root, 'codex-router.exe');
  const previousLauncher = vscode.workspace.getConfiguration('chatgpt').get('cliExecutable');
  if (previousLauncher && previousLauncher.startsWith(context.globalStorageUri.fsPath + path.sep) && previousLauncher !== launcher) {
    await vscode.workspace.getConfiguration('chatgpt').update('cliExecutable', launcher, vscode.ConfigurationTarget.Global);
  }
  const read = file => JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  const enabled = () => vscode.workspace.getConfiguration('chatgpt').get('cliExecutable') === launcher;
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 20);
  status.command = 'codexCompaction.configure';
  const refresh = () => {
    try {
      const value = read(config);
      status.text = `Compact: ${value.model.replace('gpt-5.6-', '').replace('gpt-6-', '')} / ${value.reasoning_effort}${enabled() ? '' : ' (off)'}`;
      status.tooltip = 'Codex compaction model and reasoning. Click to configure.';
    } catch (_) { status.text = 'Compact: configure'; }
    status.show();
  };
  const register = (name, handler) => context.subscriptions.push(vscode.commands.registerCommand(name, async () => {
    try { await handler(); refresh(); }
    catch (error) { vscode.window.showErrorMessage(`Compaction Router: ${error.message}`); }
  }));

  register('codexCompaction.configure', async () => {
    const current = read(config);
    const models = read(path.join(home, 'models_cache.json')).models.filter(m => m.comp_hash && m.supported_reasoning_levels?.length);
    models.sort((a, b) => Number(b.slug === current.model) - Number(a.slug === current.model));
    const chosen = await vscode.window.showQuickPick(models.map(m => ({label: m.display_name || m.slug, description: m.slug, model: m})),
      {title: 'Compaction model', placeHolder: 'The coding model stays selected in Codex.'});
    if (!chosen) return;
    const levels = chosen.model.supported_reasoning_levels;
    const effort = await vscode.window.showQuickPick(levels.map(v => ({label: v.effort, description: v.description || ''})),
      {title: `Compaction reasoning for ${chosen.label}`, placeHolder: `Current: ${current.reasoning_effort}`});
    if (!effort) return;
    const temporary = config + `.${process.pid}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify({model: chosen.model.slug, reasoning_effort: effort.label}, null, 2) + '\n');
    fs.renameSync(temporary, config);
    vscode.window.showInformationMessage(`Compaction will use ${chosen.label} / ${effort.label} on its next request.`);
  });
  register('codexCompaction.status', async () => {
    const current = read(config);
    const verification = fs.existsSync(path.join(root, 'verified.json')) ? read(path.join(root, 'verified.json')) : null;
    const content = `# Codex Compaction Router\n\nStatus: ${enabled() ? 'Enabled for new Codex backend launches' : 'Disabled'}\n\nCompaction: **${current.model} / ${current.reasoning_effort}**\n\nCoding and context are managed by the official Codex extension.\n\n${verification ? `Last compatibility check: ${verification.checked_at}\n\nChecks: ${verification.checks.join(', ')}\n\nOfficial executable: ${verification.binary}` : 'The first backend launch will run local compatibility tests.'}\n\nAfter a Codex update, the launcher finds the current official executable and repeats the tests. An incompatible protocol stops with an error.\n`;
    const doc = await vscode.workspace.openTextDocument({content, language: 'markdown'});
    await vscode.window.showTextDocument(doc);
  });
  register('codexCompaction.log', async () => {
    const log = path.join(root, 'routing.jsonl');
    if (!fs.existsSync(log)) return vscode.window.showInformationMessage('No routed requests yet. Reload VS Code to start the configured backend.');
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(log));
  });
  register('codexCompaction.enable', async () => {
    if (!fs.existsSync(launcher)) throw new Error('The local router launcher is missing. Reinstall the package.');
    await vscode.workspace.getConfiguration('chatgpt').update('cliExecutable', launcher, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage('Compaction Router enabled. Reload VS Code to apply it to existing Codex sessions.');
  });
  register('codexCompaction.disable', async () => {
    if (enabled()) await vscode.workspace.getConfiguration('chatgpt').update('cliExecutable', undefined, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage('Compaction Router disabled. Reload VS Code to restore the default backend.');
  });
  context.subscriptions.push(status, vscode.workspace.onDidChangeConfiguration(refresh));
  const watcher = fs.watch(home, (_, name) => { if (name === 'compaction-routing.json') refresh(); });
  context.subscriptions.push({dispose: () => watcher.close()});
  refresh();
  if (!enabled() && !context.globalState.get('introduced')) {
    await context.globalState.update('introduced', true);
    const answer = await vscode.window.showInformationMessage('Codex Compact Model is ready. Enable separate compaction model selection for the official Codex extension?', 'Enable');
    if (answer === 'Enable') await vscode.commands.executeCommand('codexCompaction.enable');
  }
}

exports.activate = activate;
