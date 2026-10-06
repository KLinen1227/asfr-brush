"""Non-destructive normal inheritance and world-space reoriented detail.

No baking, new images, per-frame Python work or edits to source shader groups.
Adapters/extractors are shared by source graph and reused until it changes.
"""
import hashlib
import json
import uuid
import bpy
from bpy.app.handlers import persistent

VERSION = 1
ORIGINAL = 'PP Original Normal'
BASE = 'PP Original Strength'
DETAIL = 'PP Detail Strength'
RESULT = 'PP Resolved Normal'
CONTROL_DEFAULTS = {'normal_original_strength': 1.0, 'normal_detail_strength': 1.0}

# Shader ID custom-property pointers trigger id_us_min underflow in Blender
# 4.5.5's temporary evaluated/GPU node-tree copies, even while Main users are
# correct. Cache identity must never own an ID reference. Do not mask the log
# or repair users manually: use serializable identity only.
SOURCE_KEY = 'petrify_normal_source_key'
LEGACY_SOURCE = 'petrify_normal_source'
_source_session = uuid.uuid4().hex


def _library_key(source):
    return source.library.filepath if source.library else ''


def _read_source_key(tree):
    try:
        result = json.loads(tree.get(SOURCE_KEY, '{}'))
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def remember_source(tree, source):
    # Appended preset templates can lose their library fake-user flag. Keep
    # plugin-owned templates across save/reopen without a shader ID pointer;
    # nested dependencies stay alive through normal group-node links. Do not
    # change user-authored source groups' persistence or shader contents.
    if source.get('petrify_preset_template') and not source.library:
        source.use_fake_user = True
    tree[SOURCE_KEY] = json.dumps(dict(name=source.name_full,
        library=_library_key(source), signature=fingerprint(source),
        uid=str(source.session_uid), session=_source_session), ensure_ascii=False)
    if LEGACY_SOURCE in tree:
        del tree[LEGACY_SOURCE]


def migrate_source(tree):
    if tree.library or not (tree.get('petrify_normal_adapter') or tree.get('petrify_normal_extract')):
        return False
    if LEGACY_SOURCE not in tree:
        return False
    source = tree.get(LEGACY_SOURCE)
    if isinstance(source, bpy.types.NodeTree):
        remember_source(tree, source)
    else:
        del tree[LEGACY_SOURCE]
    return True


def same_source(tree, source):
    migrate_source(tree)
    key = _read_source_key(tree)
    if key.get('session') == _source_session:
        return key.get('uid') == str(source.session_uid)
    return (key.get('name') == source.name_full and key.get('library') == _library_key(source)
            and key.get('signature') == fingerprint(source))


@persistent
def migrate_legacy_sources(_unused=None):
    """Metadata-only migration. Never rebuild materials, masks or user nodes."""
    groups = getattr(bpy.data, 'node_groups', None)
    if groups is None:
        return 0
    by_uid = {str(t.session_uid): t for t in groups}
    by_name = {(t.name_full, _library_key(t)): t for t in groups}
    migrated = 0
    for tree in groups:
        if tree.library or not (tree.get('petrify_normal_adapter') or tree.get('petrify_normal_extract')):
            continue
        if migrate_source(tree):
            migrated += 1
            continue
        key = _read_source_key(tree)
        # Update renamed sources before save; rebind identity after load/undo
        # only when the source graph matches. Deleted sources are not guessed.
        source = by_uid.get(key.get('uid')) if key.get('session') == _source_session else None
        if source is None:
            candidate = by_name.get((key.get('name'), key.get('library')))
            if candidate and key.get('signature') == fingerprint(candidate):
                source = candidate
        if source is not None:
            # Keep the cached graph signature: metadata refresh is not a shader rebuild.
            key.update(name=source.name_full, library=_library_key(source),
                       uid=str(source.session_uid), session=_source_session)
            tree[SOURCE_KEY] = json.dumps(key, ensure_ascii=False)
    return migrated


@persistent
def begin_source_session(_unused=None):
    global _source_session
    _source_session = uuid.uuid4().hex


def migrate_deferred():
    if not hasattr(bpy.data, 'node_groups'):
        return .1
    migrate_legacy_sources()
    return None


def _source_handlers():
    return ((bpy.app.handlers.load_pre, begin_source_session),
            (bpy.app.handlers.load_post, migrate_legacy_sources),
            (bpy.app.handlers.undo_post, migrate_legacy_sources),
            (bpy.app.handlers.redo_post, migrate_legacy_sources),
            (bpy.app.handlers.save_pre, migrate_legacy_sources))


def register():
    for handlers, callback in _source_handlers():
        if callback not in handlers:
            handlers.append(callback)
    if hasattr(bpy.data, 'node_groups'):
        migrate_legacy_sources()
    elif not bpy.app.timers.is_registered(migrate_deferred):
        bpy.app.timers.register(migrate_deferred, first_interval=.1)


def unregister():
    if bpy.app.timers.is_registered(migrate_deferred):
        bpy.app.timers.unregister(migrate_deferred)
    for handlers, callback in _source_handlers():
        if callback in handlers:
            handlers.remove(callback)



def node(t, kind, name):
    n = t.nodes.new(kind); n.name = name; n.label = name
    return n


def connect(t, value, target):
    if isinstance(value, bpy.types.NodeSocket): t.links.new(value, target)
    else: target.default_value = value


def vector(t, op, a, b=None, scale=None):
    n = node(t, 'ShaderNodeVectorMath', op); n.operation = op
    connect(t, a, n.inputs[0])
    if b is not None: connect(t, b, n.inputs[1])
    if scale is not None: connect(t, scale, n.inputs['Scale'])
    return n.outputs['Value' if op in {'DOT_PRODUCT', 'LENGTH'} else 'Vector']


def math(t, op, a, b=0):
    n = node(t, 'ShaderNodeMath', op); n.operation = op
    connect(t, a, n.inputs[0]); connect(t, b, n.inputs[1])
    return n.outputs[0]


def mix_vector(t, a, b, factor):
    return vector(t, 'ADD', vector(t, 'SCALE', a, scale=math(t, 'SUBTRACT', 1, factor)),
                  vector(t, 'SCALE', b, scale=factor))


def socket(t, name, kind='NodeSocketVector', default=None, direction='INPUT'):
    old = next((s for s in t.interface.items_tree if s.item_type == 'SOCKET'
                and s.in_out == direction and s.name == name), None)
    if old: return old
    s = t.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    if default is not None: s.default_value = default
    if kind == 'NodeSocketFloat': s.min_value = 0; s.max_value = 1
    return s


def interface(t):
    socket(t, ORIGINAL)
    # Zero by default leaves shared/legacy assets visually unchanged until bound.
    socket(t, BASE, 'NodeSocketFloat', 0)
    socket(t, DETAIL, 'NodeSocketFloat', 1)


def safe_normal(t, value, geometric):
    # Unlinked group vector inputs are zero, not an implicit surface normal.
    valid = math(t, 'GREATER_THAN', vector(t, 'LENGTH', value), 1e-6)
    return vector(t, 'NORMALIZE', mix_vector(t, geometric, value, valid))


def blend_group():
    for g in bpy.data.node_groups:
        if g.get('petrify_normal_blend') == VERSION: return g
    t = bpy.data.node_groups.new('.PP Reoriented Normals', 'ShaderNodeTree')
    t['petrify_normal_blend'] = VERSION
    interface(t); socket(t, 'Detail Normal'); socket(t, 'Normal', direction='OUTPUT')
    i = node(t, 'NodeGroupInput', 'Inputs')
    out = node(t, 'NodeGroupOutput', 'Output')
    geom = node(t, 'ShaderNodeNewGeometry', 'Shading Normal').outputs['Normal']
    base = safe_normal(t, i.outputs[ORIGINAL], geom)
    detail = safe_normal(t, i.outputs['Detail Normal'], geom)
    base = safe_normal(t, mix_vector(t, geom, base, i.outputs[BASE]), geom)
    detail = safe_normal(t, mix_vector(t, geom, detail, i.outputs[DETAIL]), geom)
    # Shortest-arc rotation from geometric normal to the character's normal.
    # Both inputs are shading/world-space vectors, so differing UV maps are fine.
    axis = vector(t, 'CROSS_PRODUCT', geom, base)
    cross = vector(t, 'CROSS_PRODUCT', axis, detail)
    denominator = math(t, 'ADD', 1, vector(t, 'DOT_PRODUCT', geom, base))
    inverse = math(t, 'DIVIDE', 1, math(t, 'MAXIMUM', denominator, 1e-5))
    rotation = vector(t, 'ADD', vector(t, 'ADD', detail, cross),
                      vector(t, 'SCALE', vector(t, 'CROSS_PRODUCT', axis, cross), scale=inverse))
    # Deterministic antipodal fallback, including negative/extreme authored normals.
    tangent = vector(t, 'CROSS_PRODUCT', geom, (0, 0, 1))
    tangent = safe_normal(t, tangent, vector(t, 'NORMALIZE', vector(t, 'CROSS_PRODUCT', geom, (0, 1, 0))))
    opposite = vector(t, 'SUBTRACT', vector(t, 'SCALE', tangent,
                      scale=math(t, 'MULTIPLY', 2, vector(t, 'DOT_PRODUCT', tangent, detail))), detail)
    result = mix_vector(t, opposite, rotation, math(t, 'GREATER_THAN', denominator, 1e-5))
    t.links.new(vector(t, 'NORMALIZE', result), out.inputs['Normal'])
    return t


def fingerprint_value(value):
    # RNA repr includes memory addresses; those cannot identify a saved graph.
    if isinstance(value, bpy.types.ID):
        return (value.bl_rna.identifier, value.name_full, _library_key(value))
    return str(value)


def fingerprint(t, seen=None):
    seen = set() if seen is None else seen
    if t.as_pointer() in seen: return 'cycle'
    seen.add(t.as_pointer())
    rows = []
    for n in t.nodes:
        props = [(k, fingerprint_value(getattr(n, k))) for k in ('operation', 'blend_type', 'space', 'uv_map',
                 'noise_dimensions', 'feature', 'distance', 'normalize', 'invert', 'image') if hasattr(n, k)]
        values = [(s.identifier, tuple(s.default_value) if hasattr(s.default_value, '__len__') and not isinstance(s.default_value, str) else str(s.default_value)) for s in (*n.inputs, *n.outputs) if hasattr(s, 'default_value')]
        rows.append((n.name, n.bl_idname, props, values,
                     fingerprint(n.node_tree, seen.copy()) if n.type == 'GROUP' and n.node_tree else ''))
    links = [(l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier) for l in t.links]
    return hashlib.sha256(repr((rows, links)).encode()).hexdigest()


def assign_tree(n, new):
    """Retain all matching per-instance values and incoming links on replacement."""
    saved = [(s.name, s.default_value[:] if hasattr(s, 'default_value') and hasattr(s.default_value, '__len__')
              and not isinstance(s.default_value, str) else getattr(s, 'default_value', None),
              s.links[0].from_socket if s.is_linked else None) for s in n.inputs]
    n.node_tree = new
    for name, value, source in saved:
        s = n.inputs.get(name)
        if s is None: continue
        if source: n.id_data.links.new(source, s)
        elif value is not None and hasattr(s, 'default_value'): s.default_value = value


def instrument(t):
    """Instrument only an owned copy; retain existing normal chains as detail."""
    if t.get('petrify_normal_instrumented') == VERSION: return
    interface(t)
    inp = next((n for n in t.nodes if n.type == 'GROUP_INPUT'), None) or node(t, 'NodeGroupInput', 'Normal Controls')
    for n in list(t.nodes):
        if n.type == 'GROUP' and n.node_tree and not n.node_tree.get('petrify_normal_blend'):
            if not has_surface(n.node_tree): continue
            assign_tree(n, adapt(n.node_tree))
            for k in (ORIGINAL, BASE, DETAIL): t.links.new(inp.outputs[k], n.inputs[k])
        elif n.bl_idname.startswith('ShaderNodeBsdf') or n.bl_idname == 'ShaderNodeSubsurfaceScattering':
            for name in ('Normal', 'Coat Normal'):
                target = n.inputs.get(name)
                if target is None: continue
                source = target.links[0].from_socket if target.is_linked else None
                g = node(t, 'ShaderNodeGroup', 'PP Normal · ' + n.name + ' · ' + name)
                g.node_tree = blend_group()
                for k in (ORIGINAL, BASE, DETAIL): t.links.new(inp.outputs[k], g.inputs[k])
                if source: t.links.new(source, g.inputs['Detail Normal'])
                t.links.new(g.outputs['Normal'], target)
    t['petrify_normal_instrumented'] = VERSION


def has_surface(t, seen=None):
    seen = set() if seen is None else seen
    if t.as_pointer() in seen: return False
    seen.add(t.as_pointer())
    return any((n.bl_idname.startswith('ShaderNodeBsdf') and n.inputs.get('Normal') is not None)
               or n.bl_idname == 'ShaderNodeSubsurfaceScattering'
               or (n.type == 'GROUP' and n.node_tree and has_surface(n.node_tree, seen)) for n in t.nodes)


def adapt(source):
    if source.get('petrify_normal_adapter') == VERSION:
        migrate_source(source)
        return source
    for t in bpy.data.node_groups:
        if t.get('petrify_normal_adapter') == VERSION and same_source(t, source): return t
    t = source.copy(); t.name = '.PP Normal · ' + source.name; t.use_fake_user = False
    for k in list(t.keys()):
        if k.startswith('petrify_') and k not in {'petrify_normal_blend', 'petrify_normal_instrumented'}: del t[k]
    t['petrify_normal_adapter'] = VERSION; remember_source(t, source)
    instrument(t)
    return t


def ensure_controller(t):
    preset = t.nodes.get('Preset Material')
    if not preset or not preset.node_tree: return False
    migrate_source(preset.node_tree)
    interface(t)
    assign = not preset.node_tree.get('petrify_normal_adapter')
    if assign: assign_tree(preset, adapt(preset.node_tree))
    inp = t.nodes.get('Model Alpha') or next(n for n in t.nodes if n.type == 'GROUP_INPUT')
    for k in (ORIGINAL, BASE, DETAIL):
        if not preset.inputs[k].is_linked: t.links.new(inp.outputs[k], preset.inputs[k])
    return assign


def original_roots(t):
    mixes = [n for n in t.nodes if n.type == 'MIX_SHADER' and n.name.startswith('Petrify Mix')]
    if mixes: return [l.from_socket for n in mixes for l in n.inputs[1].links]
    return [l.from_socket for n in t.nodes if n.type == 'OUTPUT_MATERIAL'
            and n.is_active_output for l in n.inputs['Surface'].links]


def match(sockets, source):
    return next((s for s in sockets if s.identifier == source.identifier), None) or sockets.get(source.name)


def resolve_input(s, path):
    if not s.is_linked: return None
    out = s.links[0].from_socket; n = out.node
    if n.type == 'REROUTE': return resolve_input(n.inputs[0], path)
    if n.type == 'GROUP_INPUT' and path:
        parent = match(path[-1].inputs, out)
        return resolve_input(parent, path[:-1]) if parent else None
    return (path, out)


def find_source(t):
    candidates = {}; unsupported = False; visited = set()
    def walk(out, path=()):
        nonlocal unsupported
        key = (tuple(n.as_pointer() for n in path), out.as_pointer())
        if key in visited: return
        visited.add(key)
        if len(path) > 24 or len(visited) > 2048: unsupported = True; return
        n = out.node
        if n.type == 'GROUP' and n.node_tree:
            groupout = next((x for x in n.node_tree.nodes if x.type == 'GROUP_OUTPUT' and x.is_active_output), None)
            inp = match(groupout.inputs, out) if groupout else None
            if inp and inp.is_linked: walk(inp.links[0].from_socket, path + (n,))
            else: unsupported = True
        elif n.type == 'GROUP_INPUT' and path:
            inp = match(path[-1].inputs, out)
            if inp and inp.is_linked: walk(inp.links[0].from_socket, path[:-1])
        elif n.type in {'MIX_SHADER', 'ADD_SHADER', 'REROUTE'}:
            for s in n.inputs:
                if s.type == 'SHADER' or n.type == 'REROUTE':
                    for l in s.links: walk(l.from_socket, path)
        elif n.inputs.get('Normal') is not None and (n.bl_idname.startswith('ShaderNodeBsdf') or n.bl_idname == 'ShaderNodeSubsurfaceScattering'):
            feed = resolve_input(n.inputs['Normal'], path)
            key = (tuple(x.name for x in feed[0]), feed[1].node.name, feed[1].identifier) if feed else ('geometry',)
            candidates[key] = feed
        elif n.type not in {'EMISSION', 'BSDF_TRANSPARENT', 'HOLDOUT'}: unsupported = True
    for root in original_roots(t): walk(root)
    if unsupported: return None, 'UNSUPPORTED'
    if len(candidates) > 1: return None, 'AMBIGUOUS'
    feed = next(iter(candidates.values()), None)
    if feed and feed[0]:
        # Internal animated group snapshots cannot safely mirror arbitrary RNA
        # paths without per-frame Python; prefer an explicit fallback.
        trees = [g.id_data for g in feed[0]] + [feed[1].id_data]
        if any(t.animation_data and (t.animation_data.action or t.animation_data.drivers) for t in trees):
            return None, 'UNSUPPORTED'
    return feed, 'DETAIL' if feed else 'GEOMETRY'


def prune(t, roots):
    keep = set(); stack = list(roots)
    while stack:
        n = stack.pop()
        if n in keep: continue
        keep.add(n)
        for s in n.inputs: stack.extend(l.from_node for l in s.links)
    for n in list(t.nodes):
        if n not in keep: t.nodes.remove(n)


def extractor(source, path, output):
    """A private, pruned copy exposes an internal normal without editing sources."""
    spec = repr(([n.name for n in path], output.node.name, output.identifier))
    sig = fingerprint(source)
    for t in bpy.data.node_groups:
        if t.get('petrify_normal_extract') == VERSION and same_source(t, source) and t.get('normal_spec') == spec and t.get('normal_signature') == sig: return t
    t = source.copy(); t.name = '.PP Source Normal · ' + source.name; t.use_fake_user = False
    for k in list(t.keys()):
        if k.startswith('petrify_') and k not in {'petrify_normal_blend', 'petrify_normal_instrumented'}: del t[k]
    t['petrify_normal_extract'] = VERSION; remember_source(t, source)
    t['normal_spec'] = spec; t['normal_signature'] = sig
    if path:
        child = t.nodes[path[0].name]
        assign_tree(child, extractor(path[0].node_tree, path[1:], output))
        feed = child.outputs[RESULT]
    else: feed = match(t.nodes[output.node.name].outputs, output)
    for n in list(t.nodes):
        if n.type == 'GROUP_OUTPUT': t.nodes.remove(n)
    for s in list(t.interface.items_tree):
        if s.item_type == 'SOCKET' and s.in_out == 'OUTPUT': t.interface.remove(s)
    socket(t, RESULT, direction='OUTPUT')
    out = node(t, 'NodeGroupOutput', 'Extracted Normal')
    t.links.new(feed, out.inputs[RESULT]); prune(t, [out])
    return t


def bind(material, target):
    s = target.inputs.get(ORIGINAL)
    if s is None: return
    t = material.node_tree
    for l in tuple(s.links): t.links.remove(l)
    s.default_value = (0, 0, 0)
    feed, status = find_source(t)
    material['petrify_normal_status'] = status
    old = t.nodes.get('PP Source Normal')
    if feed:
        path, out = feed
        if path:
            g = old or node(t, 'ShaderNodeGroup', 'PP Source Normal')
            g.node_tree = extractor(path[0].node_tree, path[1:], out)
            for source in path[0].inputs:
                dest = match(g.inputs, source)
                if dest is None: continue
                for l in tuple(dest.links): t.links.remove(l)
                if source.is_linked: t.links.new(source.links[0].from_socket, dest)
                elif hasattr(source, 'default_value'): dest.default_value = source.default_value
            out = g.outputs[RESULT]
        elif old: t.nodes.remove(old)
        t.links.new(out, s)
    elif old: t.nodes.remove(old)


def config(ob):
    d = dict(CONTROL_DEFAULTS); d.update(dict(ob.get('petrify_normal_settings', {})))
    return d


def sync(settings, ob):
    for k, v in config(ob).items(): setattr(settings, k, v)


def apply_values(ob):
    values = config(ob)
    for mat in ob.data.materials:
        if mat and mat.use_nodes and (rock := mat.node_tree.nodes.get('Petrify Stone')):
            if BASE in rock.inputs:
                rock.inputs[BASE].default_value = values['normal_original_strength']
                rock.inputs[DETAIL].default_value = values['normal_detail_strength']
    c = ob.get('petrify_ice_object')
    if isinstance(c, bpy.types.Object):
        for mat in c.get('petrify_ice_materials', []):
            g = mat.node_tree.nodes.get('Independent Ice')
            if g and BASE in g.inputs:
                g.inputs[BASE].default_value = values['normal_original_strength']
                g.inputs[DETAIL].default_value = values['normal_detail_strength']
        mat = c.get('petrify_ice_spike_material')
        if mat and (g := mat.node_tree.nodes.get('Ice')) and DETAIL in g.inputs:
            g.inputs[DETAIL].default_value = values['normal_detail_strength']
            g.inputs[BASE].default_value = 0  # Never sample character UVs on spikes.


def ensure(ob, controller, force=False):
    changed = ensure_controller(controller)
    for mat in ob.data.materials:
        if not mat or not mat.use_nodes: continue
        rock = mat.node_tree.nodes.get('Petrify Stone')
        if rock and (force or changed or 'petrify_normal_status' not in mat): bind(mat, rock)
    apply_values(ob)


def update(settings, ob, controller):
    ob['petrify_normal_settings'] = {k: getattr(settings, k) for k in CONTROL_DEFAULTS}
    ensure(ob, controller)


def cleanup_extractors():
    # Only superseded extractor copies; keep adapter caches stable across switches.
    while True:
        stale = [t for t in bpy.data.node_groups if t.get('petrify_normal_extract') and t.users == 0 and not t.use_fake_user]
        if not stale: break
        for t in stale: bpy.data.node_groups.remove(t)
