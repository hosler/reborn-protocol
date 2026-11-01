"""GS1 vetoes take precedence over every source-routing signal."""

import re
from pathlib import Path

import pytest

from reborn_protocol.gs2.detect import looks_like_gs2

WORKSPACE = Path(__file__).resolve().parents[2]
CITY_ROOT = WORKSPACE / 'Preagonal/graal-gta'
ADVENTURE_ROOT = WORKSPACE / 'Preagonal/graal-lttp'

# Reviewed conversions use GS2 event functions and function-call commands.
# Hybrid scripts with bare GS1 commands remain subject to every veto.
CITY_GS2_NPCS = {
    CITY_ROOT / 'world' / f'classic_eternia{number}.nw': set(indices)
    for number, indices in {
        1: [37], 2: [7, 21, 49], 3: [43, 44, 45], 5: [89], 6: [45],
        7: [78, 79], 8: [67, 68, 69, 70], 10: [61, 62, 63, 64], 15: [22],
    }.items()
}
CITY_GS2_NPCS.update({
    CITY_ROOT / 'world/eterniapharm.nw': {3},
    CITY_ROOT / 'world/ragnarok_eternia3.nw': {0, 1, 2, 3},
})


def npc_scripts(path):
    source = path.read_text(encoding='latin-1')
    return re.findall(r'(?ms)^NPC [^\n]*\n(.*?)^NPCEND\b', source)


@pytest.mark.parametrize('source', [
    'if (keydown(0)) {\n this.a=1;\n}',
    'if (mousedown) { this.n = this.n + 1; }',
    'if (isweapon) {this.t=1;}',
    'if (score>0) { this.css=1; play x.mid; }',
    'if (created){ timeout=.05; }\n//#CLIENTSIDE\n'
    'setani idle,;\nfunction JumpUp() {}',
    'if(created){ showcharacter; setcharprop #3,x.png; }\n//#CLIENTSIDE',
    '//#CLIENTSIDE\nfunction JumpUp() { this.x=1; }',
    '//#CLIENTSIDE\nfunction helper(a, b) { this.x=a+b; }',
    'this.x = 1;',
    'function onCreated() {}\nif(any_condition) { this.a=1; }',
    'function onCreated() {}\nif (keydown(0)) { this.a=1; }',
    'function onCreated() { this.x = #cText; }',
])
def test_gs1_reproductions(source):
    assert looks_like_gs2(source) is False


@pytest.mark.parametrize('command', [
    'setani idle,', 'showcharacter', 'setcharprop #3,x.png',
    'setstring client.x,value', 'addstring client.x,value', 'toweapons Sword',
    'message ready', 'say2 ready', 'say ready', 'play x.mid', 'showimg 1,x.png,0,0',
    'hideimg 1', 'putnpc x.png,x.txt,0,0', 'destroy', 'setplayerprop #c,ready',
    'setlevel next.nw', 'lay rupee',
    'take rupee', 'hitplayer 1,1,0,0', 'triggeraction 0,0,ready',
    'setimg waterani.mng', 'dontblock', 'drawoverplayer', 'noplayerkilling',
])
@pytest.mark.parametrize('signal', [
    'function onCreated() {}', '//#CLIENTSIDE\nfunction helper(a) { return a; }',
])
def test_commands_veto_gs2_signals(command, signal):
    assert not looks_like_gs2(signal + '\nfunction helper2() { ' + command + '; }')


@pytest.mark.parametrize('assignment', ['timeout = 0.05;', 'timeout=.05;',
                                       'TIMEOUT = 1;'])
@pytest.mark.parametrize('template,expected', [
    ('{} function onCreated() {{}}', False),
    ('function onCreated() {{}} {}', False),
    ('function onCreated() {{}} {{ {} }}', False),
    ('function onCreated() {{ function helper() {{}} }} {{ {} }}', False),
    ('new TStaticVar() {{ {} }}', False),
    ('if (created) {{ {} }} function onCreated() {{}}', False),
    ('function onCreated() {{ {} }}', True),
    ('function onCreated() {{ if (ready) {} }}', True),
    ('function onTimeout() {{ if (ready) {{ {} }} }}', True),
    ('public function helper() {{ {} }}', True),
    ('function helper() {{ {} return 1; }}', True),
    ('function onCreated() {{ function helper() {{}} {} }}', True),
    ('function onCreated() {{}} function helper() {{ {} }}', True),
    ('function onCreated() {{ {} }} if (ready) {{}}', False),
    ('function onCreated() {{ {} setani idle,; }}', False),
    ('function onCreated() {{ {} temp.x = #v(x); }}', False),
    ('// function helper() {{\n{} function onCreated() {{}}', False),
    ('"function helper() {{"; {} function onCreated() {{}}', False),
    ('function onCreated() {{ temp.x = "}}"; /* }} */ {} }}', True),
])
def test_numeric_timeout_veto_only_outside_functions(assignment, template, expected):
    assert looks_like_gs2(template.format(assignment)) is expected


@pytest.mark.parametrize('substitution', ['#v(x)', '#s(x)', '#e(0,1,x)', '#I(x)', '#c'])
def test_substitutions_veto_event_functions(substitution):
    assert not looks_like_gs2('function onCreated() { temp.x=' + substitution + '; }')


@pytest.mark.parametrize('source', [
    'function onCreated() {}', 'new TStaticVar() {}', 'new GuiWindowCtrl("x") {}',
    'public function helper() {}', 'with (SomeControl) {}', 'temp.x = 1;',
    'foreach (temp.item: items) {}', 'this.helper();',
    'function onCreated() { if (ready) { temp.x = 1; } }',
    'function onCreated() { triggeraction(0,0,"ready"); }',
    'function onCreated() { triggeraction \n (0,0,"ready"); }',
    'function onCreated() { showimg(1,"x.png",0,0); hideimg(1); destroy(); }',
    'function onCreated() { this.timeout = .05; }',
    'function helper() { this.helper(); }',
    'function helper(a) { return a * 2; }',
    '// if (created) { setani idle,; }\nfunction onCreated() {}',
    '/* if (created) {} */ function onCreated() { temp.x="#c"; }',
    'function onCreated() { temp.x="} if (created) {"; }',
    "function onCreated() { temp.x='setani idle,;'; }",
])
def test_positive_syntax_without_veto(source):
    assert looks_like_gs2(source)


@pytest.mark.parametrize('source', [
    '//#CLIENTSIDE', '// function onCreated() {}',
    '/* new GuiControl("x") {} */', '"function onCreated() {}"',
    "'function onCreated() {}'", 'this.x = 1; // temp.x = 1;',
    'function helper() {}', '/* function onCreated() {}',
    '/* function onCreated() {}\n/*',
])
def test_strings_comments_and_shared_syntax_are_not_evidence(source):
    assert not looks_like_gs2(source)


def check_npc_corpus(paths, label, gs2_cases=None):
    gs2_cases = gs2_cases or {}
    files = scripts = gs2_count = 0
    failures = []
    seen_gs2 = set()
    for path in paths:
        files += 1
        for index, source in enumerate(npc_scripts(path)):
            if not source.strip():
                continue
            scripts += 1
            expected = index in gs2_cases.get(path, set())
            actual = looks_like_gs2(source)
            gs2_count += actual
            if expected:
                seen_gs2.add((path, index))
            if actual != expected:
                failures.append(f'{path.relative_to(WORKSPACE)}#{index}:\n{source}')
    print(f'{label}: {files} files, {scripts} nonempty NPC scripts, '
          f'{scripts - gs2_count} GS1, {gs2_count} GS2')
    assert scripts, f'{label}: no NPC scripts found'
    assert seen_gs2 == {(path, index) for path, indices in gs2_cases.items()
                        for index in indices}, 'reviewed GS2 NPC missing'
    assert not failures, '\n\n'.join(failures)


@pytest.mark.skipif(not (WORKSPACE / 'funtimes/world/ball_game.nw').is_file(),
                    reason='optional ball-game level absent')
def test_ball_game_npc_scripts():
    check_npc_corpus([WORKSPACE / 'funtimes/world/ball_game.nw'], 'ball-game')


@pytest.mark.skipif(not (WORKSPACE / 'funtimes/world').is_dir(),
                    reason='optional GS1 world absent')
def test_funtimes_npc_corpus():
    # The chat-triggered warp is active GS2; the scoreboard's bare
    # noplayerkilling command makes that separate hybrid script select GS1.
    check_npc_corpus(sorted((WORKSPACE / 'funtimes/world').glob('*.nw')), 'funtimes',
                     {WORKSPACE / 'funtimes/world/ball_lobby.nw': {3}})


@pytest.mark.skipif(not CITY_ROOT.is_dir(),
                    reason='optional city-world checkout absent')
def test_city_npc_corpus():
    check_npc_corpus(sorted(CITY_ROOT.rglob('*.nw')), 'city', CITY_GS2_NPCS)


@pytest.mark.skipif(not ADVENTURE_ROOT.is_dir(),
                    reason='optional adventure-world checkout absent')
def test_adventure_weapon_corpus():
    paths = sorted(path for path in ADVENTURE_ROOT.glob('**/weapons/*')
                   if path.is_file())
    # These GS2 event scripts assign bare numeric timeouts inside functions.
    timeout_weapons = {'weaponBottle HP.txt', 'weaponBottle MP.txt',
                       'weaponMagic Cape.txt'}
    seen_timeouts = set()
    gs2_count = 0
    failures = []
    for path in paths:
        source = path.read_text(encoding='latin-1')
        match = re.search(r'(?ms)^SCRIPT\n(.*?)^SCRIPTEND\b', source)
        assert match, f'weapon source block missing: {path}'
        actual = looks_like_gs2(match[1])
        gs2_count += actual
        if path.name in timeout_weapons:
            seen_timeouts.add(path.name)
            assert re.search(r'\btimeout\s*=\s*[\d.]', match[1])
        if not actual:
            failures.append(f'{path.relative_to(WORKSPACE)}:\n{match[1]}')
    print(f'adventure: {len(paths)} weapons, {gs2_count} GS2, '
          f'{len(paths) - gs2_count} vetoed')
    assert len(paths) == gs2_count == 16
    assert seen_timeouts == timeout_weapons, 'reviewed timeout weapon missing'
    assert not failures, '\n\n'.join(failures)
