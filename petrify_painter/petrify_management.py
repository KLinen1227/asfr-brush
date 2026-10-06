"""ASFR preset memory and copy-on-write batch transactions.

Original object identities, rigs, animation and scene materials outside the
target set are not replaced. No undo operators or global orphan purge are used.
"""
import bpy
import json
from contextlib import contextmanager


def remember(tree):
    if not tree or not tree.get('petrify_controller'):
        return
    node = tree.nodes.get('Preset Material')
    if not node:
        return
    data = json.loads(tree.get('asfr_look_memory', '{}'))
    values = {}
    for s in node.inputs:
        if not s.is_linked and hasattr(s, 'default_value'):
            v = s.default_value
            if isinstance(v, (bool, int, float, str)):
                values[s.name] = v
            elif hasattr(v, '__len__') and not isinstance(v, bpy.types.ID):
                values[s.name] = list(v)
    data[tree.get('petrify_preset', 'DEFAULT')] = {
        'inputs': values,
        'resin': {k: tree[k] for k in ('petrify_resin_color_mode', 'petrify_resin_blend') if k in tree},
    }
    tree['asfr_look_memory'] = json.dumps(data)


def restore(tree, preset):
    data = json.loads(tree.get('asfr_look_memory', '{}')).get(preset, {})
    node = tree.nodes.get('Preset Material')
    if node:
        for name, value in data.get('inputs', {}).items():
            sock = node.inputs.get(name)
            if sock and not sock.is_linked and hasattr(sock, 'default_value'):
                sock.default_value = value
    for key, value in data.get('resin', {}).items():
        tree[key] = value


def switch_object_memory(ob, current, preset, api, reset=False):
    memory = json.loads(ob.get('asfr_object_memory', '{}'))
    if current:
        memory[current] = {'normal': api.normals.config(ob), 'ice': api.ice.config(ob)}
    if reset:
        memory.pop(preset, None)
        tree = api.stone_for(ob)
        if tree:
            values = json.loads(tree.get('asfr_look_memory', '{}'))
            values.pop(preset, None)
            tree['asfr_look_memory'] = json.dumps(values)
    saved = memory.get(preset, {})
    ob['petrify_normal_settings'] = saved.get('normal', dict(api.normals.CONTROL_DEFAULTS))
    if preset == api.ice.KEY:
        ob['petrify_ice_settings'] = saved.get('ice', dict(api.ice.DEFAULTS))
    ob['asfr_object_memory'] = json.dumps(memory, default=lambda x: x.to_dict() if hasattr(x, 'to_dict') else list(x))


def preflight(context, objects, api, prepare=False):
    if context.mode != 'OBJECT':
        raise RuntimeError('请先退出绘制或编辑模式，再批量初始化/切换材质')
    for ob in objects:
        if ob.library or ob.data.library or not ob.is_editable:
            raise RuntimeError('目标为链接或不可编辑数据：' + ob.name)
        if not ob.data.polygons:
            raise RuntimeError('目标没有可绘制面：' + ob.name)
        if prepare and not api.mask_for(ob):
            if len(ob.data.uv_layers) >= 8 and api.UV_NAME not in ob.data.uv_layers:
                raise RuntimeError('UV 层已满：' + ob.name)
            for mat in ob.data.materials:
                if mat and mat.use_nodes and not any(n.type == 'OUTPUT_MATERIAL' and n.inputs['Surface'].is_linked for n in mat.node_tree.nodes):
                    raise RuntimeError('材质没有表面输出：' + mat.name)


def _properties(ob):
    return {k: (v.to_dict() if hasattr(v, 'to_dict') else v.to_list() if hasattr(v, 'to_list') else v)
            for k, v in ob.items() if k.startswith(('petrify_', 'asfr_'))}


@contextmanager
def batch(context, objects, api, prepare=False):
    """Keep old data alive until every target succeeds; restore it on failure."""
    preflight(context, objects, api, prepare)
    pools = (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.node_groups, bpy.data.images)
    before = {id(pool): {x.as_pointer() for x in pool} for pool in pools}
    fake_users = [(m, m.use_fake_user) for m in bpy.data.materials]
    active = context.view_layer.objects.active
    selected = list(context.selected_objects)
    target = context.scene.petrify_settings.target
    records = []
    success = False
    try:
        for ob in objects:
            old = ob.data
            record = dict(ob=ob, data=old, props=_properties(ob), name=old.name,
                          data_pointer=old.as_pointer(),
                          slots=[(s.link, s.material) for s in ob.material_slots],
                          controller=api.stone_for(ob), companion=api.ice.companion(ob),
                          modifiers=[(m, m.node_group) for m in ob.modifiers if api.topology.is_transfer(m)])
            record['controller_pointer'] = record['controller'].as_pointer() if record['controller'] else 0
            records.append(record)
            ob.data = old.copy()
            # Isolate all material trees before any node/link edits, including
            # object-linked material slots and duplicate slots in the same mesh.
            copies = {}
            for slot in ob.material_slots:
                mat = slot.material
                if mat:
                    copies.setdefault(mat.as_pointer(), None)
                    if copies[mat.as_pointer()] is None:
                        copies[mat.as_pointer()] = mat.copy()
                    slot.link = 'DATA'
                    slot.material = copies[mat.as_pointer()]
            tree = record['controller']
            if tree:
                new = tree.copy(); new.use_fake_user = False
                new['petrify_owner'] = ob.name
                ob['petrify_stone'] = new.name
                for mat in ob.data.materials:
                    if mat and mat.use_nodes and (rock := mat.node_tree.nodes.get('Petrify Stone')):
                        rock.node_tree = new
            if record['companion']:
                del ob['petrify_ice_object']
            store = api.topology.store_for(ob)
            if store:
                new_store = store.copy(); new_store.data = store.data.copy()
                store.users_collection[0].objects.link(new_store)
                new_store[api.topology.OWNER] = ob
                ob[api.topology.STORE_KEY] = new_store
                record['store'] = store
                for mod, group in record['modifiers']:
                    mod.node_group = group.copy()
                api.topology.sync_object(ob)
        yield
        success = True
    finally:
        if not success:
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for r in reversed(records):
                ob = r['ob']; ob.data = r['data']
                for slot, (kind, mat) in zip(ob.material_slots, r['slots']):
                    slot.link = kind; slot.material = mat
                for key in tuple(ob.keys()):
                    if key.startswith(('petrify_', 'asfr_')): del ob[key]
                for key, value in r['props'].items(): ob[key] = value
                for mod, group in r['modifiers']: mod.node_group = group
            for mat, value in fake_users: mat.use_fake_user = value
            created = [x for pool in pools for x in pool if x.as_pointer() not in before[id(pool)]]
            if created: bpy.data.batch_remove(ids=tuple(created))
        else:
            # Original companions were detached, not destroyed during staging.
            removed_meshes, removed_groups, removed_stores = set(), set(), set()
            for r in records:
                ob = r['ob']; old = r['companion']
                new = api.ice.companion(ob)
                if old:
                    ob['petrify_ice_object'] = old
                    api.ice.remove(ob)
                    if new: ob['petrify_ice_object'] = new
                old_mesh = r['data']
                old_pointer = r['data_pointer']
                if old_pointer not in removed_meshes and old_mesh.users == 0 and not old_mesh.use_fake_user:
                    removed_meshes.add(old_pointer)
                    bpy.data.meshes.remove(old_mesh)
                    ob.data.name = r['name']
            # Only release replaced plugin-owned data, never global orphans.
            old_mats = {mat for r in records for _, mat in r['slots'] if mat}
            for mat in old_mats:
                if mat.get('petrify_original') and mat.users == 0 and not mat.use_fake_user:
                    name = mat.name
                    replacement = next((m for r in records for m in r['ob'].data.materials if m and m.name.startswith(name + '.')), None)
                    bpy.data.materials.remove(mat)
                    if replacement: replacement.name = name
            for r in records:
                old = r['controller']; new = api.stone_for(r['ob'])
                if r['controller_pointer'] not in removed_groups and old and old.users == 0 and not old.use_fake_user:
                    removed_groups.add(r['controller_pointer'])
                    name = old.name; bpy.data.node_groups.remove(old)
                    new.name = name; r['ob']['petrify_stone'] = new.name
                for _, group in r['modifiers']:
                    if group and group.users == 0 and not group.use_fake_user:
                        bpy.data.node_groups.remove(group)
            for old_store in {r['store'] for r in records if r.get('store')}:
                # Old transfer graphs must be released before testing store users.
                if old_store.users == 1:
                    mesh = old_store.data
                    bpy.data.objects.remove(old_store, do_unlink=True)
                    if mesh.users == 0: bpy.data.meshes.remove(mesh)
        # Preserve the user's selection and active target, even for prepare.
        for ob in context.selected_objects: ob.select_set(False)
        for ob in selected: ob.select_set(True)
        context.view_layer.objects.active = active
        context.scene.petrify_settings.target = target
