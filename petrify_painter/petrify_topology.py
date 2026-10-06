"""Persistent masks on evaluated meshes, without applying user modifiers.

The ordinary path continues to paint the original mesh. Generated topology uses
a hidden point-data object and a final geometry-nodes attribute transfer. Index
sampling preserves independent mirrored/array copies and follows deformation.
All creation happens on commit, never on mouse movement or cancellation.
"""
from array import array
import hashlib
import json
import bpy

STORE_KEY = 'petrify_topology_store'
TAG = 'petrify_topology_data'
GROUP_TAG = 'petrify_topology_transfer'
OWNER = 'petrify_topology_owner'
COLLECTION = '石化笔刷 · 内部绘制数据'
# Fixed evaluated topology only. Simulations, arbitrary Geometry Nodes, and
# topology-changing animation need a different correspondence representation.
GENERATORS = {'SUBSURF', 'MIRROR', 'SOLIDIFY', 'ARRAY', 'BEVEL', 'TRIANGULATE',
              'EDGE_SPLIT', 'WELD', 'DECIMATE', 'BOOLEAN', 'REMESH', 'MASK',
              'MULTIRES'}


def is_transfer(modifier):
    return (modifier.type == 'NODES' and modifier.node_group is not None
            and bool(modifier.node_group.get(GROUP_TAG)))


def store_for(ob):
    value = ob.get(STORE_KEY)
    return value if isinstance(value, bpy.types.Object) and value.type == 'MESH' and value.get(TAG) else None


def data_mesh(ob):
    store = store_for(ob)
    return store.data if store else ob.data


def signature(mesh):
    """Connectivity, not positions: skinning and shape keys stay supported."""
    digest = hashlib.sha256()
    digest.update(str((len(mesh.vertices), len(mesh.edges), len(mesh.polygons))).encode())
    for collection, prop, count in ((mesh.edges, 'vertices', 2),
                                    (mesh.loops, 'vertex_index', 1),
                                    (mesh.polygons, 'loop_total', 1)):
        values = array('i', [0]) * (len(collection) * count)
        collection.foreach_get(prop, values)
        digest.update(values.tobytes())
    return digest.hexdigest()


def generator_settings(ob):
    result = []
    for m in ob.modifiers:
        if m.type not in GENERATORS:continue
        values = {'type': m.type}
        for prop in m.bl_rna.properties:
            if prop.is_readonly or prop.identifier in {'name', 'is_active', 'show_expanded', 'show_on_cage', 'show_in_editmode'}:
                continue
            value = getattr(m, prop.identifier)
            if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
                values[prop.identifier] = sorted(value) if isinstance(value, set) else list(value) if getattr(prop, 'is_array', False) else value
            elif prop.type == 'POINTER' and isinstance(value, bpy.types.ID):
                values[prop.identifier] = value.name
        result.append(values)
    return json.dumps(result, sort_keys=True)


def _animation_paths(ob):
    animation = ob.animation_data
    if not animation:return set()
    paths = {c.data_path for c in animation.drivers}
    actions = [animation.action] + [s.action for t in animation.nla_tracks for s in t.strips if s.action]
    for action in actions:
        if action is None:continue
        paths.update(c.data_path for c in getattr(action, 'fcurves', ()))
        for layer in getattr(action, 'layers', ()):
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    paths.update(c.data_path for c in bag.fcurves)
    return paths


def _simplify_settings(scene):
    r = scene.render
    return json.dumps([r.use_simplify, r.simplify_subdivision, r.simplify_subdivision_render])


def settings_issue(ob, scene, deform_modifiers):
    """Cheap checks also displayed in the panel before users start a stroke."""
    transfers = [m for m in ob.modifiers if is_transfer(m)]
    store = store_for(ob)
    if store:
        if len(transfers) != 1 or ob.modifiers[-1] != transfers[0]:
            return '请将“石化 · 修改器兼容”保留在修改器列表末尾'
        if not transfers[0].show_viewport or not transfers[0].show_render:
            return '请开启“石化 · 修改器兼容”的视图和渲染显示'
    elif transfers:
        return '找不到修改器兼容数据，请恢复内部绘制数据对象'
    if getattr(getattr(ob, 'cycles', None), 'use_adaptive_subdivision', False):
        return '请关闭自适应细分，使用固定细分级别后绘制'
    paths = _animation_paths(ob)
    for m in ob.modifiers:
        if is_transfer(m) or m.type in deform_modifiers:
            continue
        if not m.show_viewport and not m.show_render:
            continue
        if m.type not in GENERATORS:
            return '此版本暂不支持该修改器：' + m.name
        if any(path.startswith(m.path_from_id() + '.') for path in paths):
            return '生成网格的修改器参数动画暂不支持：' + m.name
        if m.type == 'BOOLEAN' and m.object and _animation_paths(m.object):
            return '当前支持静态布尔，请停用布尔参照物动画后绘制'
        if m.show_viewport != m.show_render:
            return '请让修改器的视图/渲染开关一致：' + m.name
        if m.type in {'SUBSURF', 'MULTIRES'} and m.levels != m.render_levels:
            return '请将预览和渲染细分级别设为一致：' + m.name
        if m.type == 'SUBSURF' and getattr(m, 'use_adaptive_subdivision', False):
            return '绘制兼容模式暂不支持自适应细分：' + m.name
    if scene.render.use_simplify and any(m.show_viewport and m.type in {'SUBSURF', 'MULTIRES'} for m in ob.modifiers):
        if scene.render.simplify_subdivision != scene.render.simplify_subdivision_render:
            return '请让简化设置中的视图/渲染细分上限一致'
    if store and store.get('generator_settings') != generator_settings(ob):
        return '生成网格的修改器设置已改变，请恢复绘制时的设置'
    if store and any(m.type in {'SUBSURF', 'MULTIRES'} for m in ob.modifiers):
        if store.get('simplify_settings') != _simplify_settings(scene):
            return '简化设置已改变，请恢复绘制时的细分上限'
    return None


def needs_store(ob, deform_modifiers):
    return bool(store_for(ob)) or any(m.show_viewport and not is_transfer(m)
                                     and m.type not in deform_modifiers for m in ob.modifiers)


def validate_mesh(ob, mesh):
    value = signature(mesh)
    store = store_for(ob)
    if store and store.get('topology_signature') != value:
        raise RuntimeError(ob.name + '：修改器结果网格已改变，请恢复绘制时的修改器设置；原石化数据已保留')
    return value


def _paint_attributes(mesh):
    return [a for a in mesh.attributes if a.name.startswith('Petrify_')
            and a.data_type == 'FLOAT' and a.domain == 'POINT']


def _node(tree, kind, name):
    node = tree.nodes.new(kind)
    node.name = name
    return node


def make_group(store, tree):
    tree[GROUP_TAG] = True
    tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    source = _node(tree, 'NodeGroupInput', 'Source')
    output = _node(tree, 'NodeGroupOutput', 'Output')
    info = _node(tree, 'GeometryNodeObjectInfo', 'Paint Data')
    info.inputs['Object'].default_value = store
    info.inputs['As Instance'].default_value = False
    _node(tree, 'GeometryNodeInputIndex', 'Vertex Index')
    size = _node(tree, 'GeometryNodeAttributeDomainSize', 'Topology Size')
    size.component = 'MESH'
    tree.links.new(source.outputs['Geometry'], size.inputs['Geometry'])
    count = _node(tree, 'FunctionNodeCompare', 'Topology Matches')
    count.data_type = 'INT'; count.operation = 'EQUAL'
    count.inputs[3].default_value = len(store.data.vertices)
    tree.links.new(size.outputs['Point Count'], count.inputs[2])
    switch = _node(tree, 'GeometryNodeSwitch', 'Safe Geometry')
    switch.input_type = 'GEOMETRY'
    tree.links.new(count.outputs[0], switch.inputs['Switch'])
    tree.links.new(source.outputs['Geometry'], switch.inputs['False'])
    tree.links.new(switch.outputs[0], output.inputs['Geometry'])
    sync_group(tree, store)
    return tree


def sync_group(tree, store):
    """Reuse one transfer group; add/remove only changed attribute branches."""
    names = {a.name for a in _paint_attributes(store.data)}
    tree.nodes['Paint Data'].inputs['Object'].default_value = store
    for node_name in tuple(n.name for n in tree.nodes if n.get('paint_attribute') and n['paint_attribute'] not in names):
        tree.nodes.remove(tree.nodes[node_name])
    geometry = tree.nodes['Source'].outputs['Geometry']
    for i, name in enumerate(sorted(names)):
        read = tree.nodes.get('Read ' + name)
        if read is None:
            read = _node(tree, 'GeometryNodeInputNamedAttribute', 'Read ' + name)
            read.data_type = 'FLOAT'; read.inputs['Name'].default_value = name
            sample = _node(tree, 'GeometryNodeSampleIndex', 'Sample ' + name)
            sample.data_type = 'FLOAT'; sample.domain = 'POINT'; sample.clamp = False
            write = _node(tree, 'GeometryNodeStoreNamedAttribute', 'Write ' + name)
            write.data_type = 'FLOAT'; write.domain = 'POINT'; write.inputs['Name'].default_value = name
            for node in (read, sample, write):node['paint_attribute'] = name
            tree.links.new(tree.nodes['Paint Data'].outputs['Geometry'], sample.inputs['Geometry'])
            tree.links.new(tree.nodes['Vertex Index'].outputs[0], sample.inputs['Index'])
            tree.links.new(read.outputs['Attribute'], sample.inputs['Value'])
            tree.links.new(sample.outputs[0], write.inputs['Value'])
        write = tree.nodes['Write ' + name]
        tree.links.new(geometry, write.inputs['Geometry'])
        geometry = write.outputs['Geometry']
        read.location = (-600, -i * 180)
        tree.nodes['Sample ' + name].location = (-350, -i * 180)
        write.location = (i * 220, 200)
    tree.links.new(geometry, tree.nodes['Safe Geometry'].inputs['True'])


def sync_object(ob):
    store = store_for(ob)
    if store:
        for m in ob.modifiers:
            if is_transfer(m):sync_group(m.node_group, store)
        store.data.update()
        ob.update_tag(refresh={'OBJECT', 'DATA'})


def sync_all():
    for ob in bpy.data.objects:
        if store_for(ob):sync_object(ob)


def _create_store(context, entry, tokens):
    ob = entry['ob']
    previous = store_for(ob)
    existing = next((m for m in ob.modifiers if is_transfer(m)), None)
    token = dict(ob=ob, previous=previous, modifier=existing,
                 old_group=existing.node_group if existing else None,
                 store=None, mesh=None, group=None, collection=None, created_modifier=False)
    tokens.append(token)
    mesh = bpy.data.meshes.new(ob.name + ' · 石化绘制数据')
    token['mesh'] = mesh
    mesh.vertices.add(entry['count'])
    store = bpy.data.objects.new(ob.name + ' · 石化绘制数据', mesh)
    token['store'] = store
    store[TAG] = True; store[OWNER] = ob
    store['topology_signature'] = entry['topology_signature']
    store['generator_settings'] = generator_settings(ob)
    store['simplify_settings'] = _simplify_settings(context.scene)
    collection = next((c for c in bpy.data.collections if c.get(TAG)), None)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION)
        collection[TAG] = True
        token['collection'] = collection
    if collection.name not in context.scene.collection.children:
        context.scene.collection.children.link(collection)
    collection.objects.link(store)
    store.hide_render = True; store.hide_select = True
    store.hide_set(True)
    for name, values in entry['inherited_attributes'].items():
        mesh.attributes.new(name, 'FLOAT', 'POINT').data.foreach_set('value', values)
    if 'Petrify_Volume' not in mesh.attributes:mesh.attributes.new('Petrify_Volume', 'FLOAT', 'POINT')
    group = bpy.data.node_groups.new('石化 · 修改器兼容', 'GeometryNodeTree')
    token['group'] = group
    make_group(store, group)
    if existing is None:
        existing = ob.modifiers.new('石化 · 修改器兼容', 'NODES')
        token['modifier'] = existing; token['created_modifier'] = True
    existing.node_group = group
    ob[STORE_KEY] = store
    mesh.update()


def begin_commit(context, entries, deform_modifiers):
    """Validate every target first, then allocate rollback-safe storage."""
    depsgraph = context.evaluated_depsgraph_get()
    for entry in entries:
        ob = entry['ob']
        issue = settings_issue(ob, context.scene, deform_modifiers)
        if issue:raise RuntimeError(ob.name + '：' + issue)
        evaluated = ob.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            if signature(mesh) != entry['topology_signature']:
                raise RuntimeError(ob.name + '：绘制期间网格结构发生变化，请取消后重新绘制')
        finally:evaluated.to_mesh_clear()
    tokens = []
    try:
        for entry in entries:
            if not entry['requires_store']:continue
            store = store_for(entry['ob'])
            if store is None or store.get(OWNER) != entry['ob']:
                _create_store(context, entry, tokens)
        return tokens
    except Exception:
        rollback(tokens)
        raise


def rollback(tokens):
    for token in reversed(tokens):
        ob = token['ob']
        if token['created_modifier']:ob.modifiers.remove(token['modifier'])
        elif token['modifier']:token['modifier'].node_group = token['old_group']
        if token['previous']:ob[STORE_KEY] = token['previous']
        elif STORE_KEY in ob:del ob[STORE_KEY]
        if token['group']:bpy.data.node_groups.remove(token['group'])
        if token['store']:bpy.data.objects.remove(token['store'], do_unlink=True)
        if token['mesh'] and token['mesh'].users == 0:bpy.data.meshes.remove(token['mesh'])
        if token['collection'] and not token['collection'].objects:bpy.data.collections.remove(token['collection'])


def inherited_attributes(mesh):
    result = {}
    for attr in _paint_attributes(mesh):
        values = array('f', [0]) * len(mesh.vertices)
        attr.data.foreach_get('value', values)
        result[attr.name] = values
    return result
