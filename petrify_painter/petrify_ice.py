"""Independent, topology-preserving frost shells and stable attached ice spikes.

No frame handlers, mesh writes while painting, or destructive source modifiers.
The original surface passes through unchanged. A companion reads evaluated
geometry and follows it; capped barycentric anchors are sampled only on demand.
"""
import bpy
if __package__:
    from . import petrify_normals as normals
else:
    import petrify_normals as normals
import math
import random
from bisect import bisect_left
from array import array

KEY = 'FROST'
TAG = 'petrify_ice_companion'
OWNER = 'petrify_ice_source'
SURFACE = 'Original Surface'
CAP = 1000
DEFAULTS = dict(ice_clarity=90.0, ice_frost=12.0, ice_tint=18.0,
                ice_color=(0.32, 0.7, 0.92, 1), ice_roughness=.16,
                ice_thickness=.16, ice_count=180, ice_length=6.0,
                ice_width=.65, ice_random=.6, ice_down=.85,
                ice_spike_clarity=58.0, ice_style='ICICLE', ice_seed=0,
                ice_exclude_materials='', ice_direction_mode='LEGACY',
                ice_direction_space='WORLD', ice_direction_azimuth=0.0,
                ice_direction_elevation=-math.pi/2, ice_direction_spread=math.pi/6,
                ice_direction_seed=0)


def node(t, kind, name):
    n=t.nodes.new(kind);n.name=name;n.label=name
    return n


def link(t, value, socket):
    if isinstance(value, bpy.types.NodeSocket):t.links.new(value,socket)
    else:socket.default_value=value


def calc(t, op, a, b=0, name=None):
    n=node(t,'ShaderNodeMath',name or op);n.operation=op
    link(t,a,n.inputs[0]);link(t,b,n.inputs[1]);return n.outputs[0]


def vec(t, op, a, b=None, scale=None):
    n=node(t,'ShaderNodeVectorMath',op);n.operation=op
    link(t,a,n.inputs[0])
    if b is not None:link(t,b,n.inputs[1])
    if scale is not None:link(t,scale,n.inputs['Scale'])
    return n.outputs['Vector']


def value(t,name,v):
    n=node(t,'ShaderNodeValue',name);n.outputs[0].default_value=v
    return n.outputs[0]


def socket(t,name,kind,direction='INPUT',default=None):
    s=t.interface.new_socket(name=name,in_out=direction,socket_type=kind)
    if default is not None:s.default_value=default
    return s


def template():
    for t in bpy.data.node_groups:
        if t.get('petrify_preset_template')==KEY:return t
    t=bpy.data.node_groups.new('PP Preset Clear Frost','ShaderNodeTree')
    socket(t,'Shader','NodeSocketShader','OUTPUT')
    socket(t,'Alpha','NodeSocketFloat',default=1)
    expose_surface(t)
    i=node(t,'NodeGroupInput','Unchanged original material')
    o=node(t,'NodeGroupOutput','Original material')
    t.links.new(i.outputs[SURFACE],o.inputs['Shader'])
    t['petrify_preset_template']=KEY;t['petrify_preset']=KEY
    t['petrify_preset_revision']='3.4.0';t.use_fake_user=True
    return t


def expose_surface(t):
    if not any(s.item_type=='SOCKET' and s.in_out=='INPUT' and s.name==SURFACE for s in t.interface.items_tree):
        socket(t,SURFACE,'NodeSocketShader')


def bind_surface(mat,rock):
    if SURFACE not in rock.inputs:return
    for n in mat.node_tree.nodes:
        if n.type=='MIX_SHADER' and n.name.startswith('Petrify Mix') and n.inputs[1].is_linked:
            mat.node_tree.links.new(n.inputs[1].links[0].from_socket,rock.inputs[SURFACE]);return


def companion(ob):
    x=ob.get('petrify_ice_object')
    return x if isinstance(x,bpy.types.Object) and x.get(TAG) and x.get(OWNER)==ob else None


def config(ob):
    data=dict(DEFAULTS);data.update(dict(ob.get('petrify_ice_settings',{})))
    return data


def sync(settings,ob):
    for k,v in config(ob).items():setattr(settings,k,v)


def shader(ob):
    c=companion(ob);t=c.get('petrify_ice_shader') if c else None
    if isinstance(t,bpy.types.NodeTree):
        normals.instrument(t)
        return t
    t=bpy.data.node_groups.new(ob.name+' · 冰层光学','ShaderNodeTree')
    t['petrify_ice_owned']=True
    socket(t,'Shader','NodeSocketShader','OUTPUT')
    for name,default in [('Mask',1),('Alpha',1),('Spike',0)]:socket(t,name,'NodeSocketFloat',default=default)
    i=node(t,'NodeGroupInput','Surface inputs')
    controls={k:value(t,k,0) for k in ('ice_clarity','ice_frost','ice_tint','ice_roughness','ice_spike_clarity','bump_distance')}
    color=node(t,'ShaderNodeRGB','ice_color').outputs[0]
    coord=node(t,'ShaderNodeTexCoord','Ice coordinates')
    noise=node(t,'ShaderNodeTexNoise','Frost patches');noise.inputs['Scale'].default_value=22
    noise.inputs['Detail'].default_value=3;noise.inputs['Roughness'].default_value=.7
    t.links.new(coord.outputs['Generated'],noise.inputs['Vector'])
    fine=node(t,'ShaderNodeTexNoise','Frost grains');fine.inputs['Scale'].default_value=220
    fine.inputs['Detail'].default_value=2;t.links.new(coord.outputs['Generated'],fine.inputs['Vector'])
    patch=calc(t,'MULTIPLY',calc(t,'SUBTRACT',noise.outputs['Fac'],.38),5)
    patch.node.use_clamp=True
    # Frost opacity is bounded by the slider, rather than becoming an opaque
    # white statue at moderate percentages. Zero is genuinely frost-free.
    patch=calc(t,'MULTIPLY',patch,controls['ice_frost'])
    bump=node(t,'ShaderNodeBump','Micro frost');bump.inputs['Strength'].default_value=.22
    t.links.new(fine.outputs['Fac'],bump.inputs['Height']);t.links.new(controls['bump_distance'],bump.inputs['Distance'])
    palette=node(t,'ShaderNodeMixRGB','Ice tint');palette.inputs[1].default_value=(.55,.78,.94,1)
    t.links.new(controls['ice_tint'],palette.inputs[0]);t.links.new(color,palette.inputs[2])
    ice=node(t,'ShaderNodeBsdfPrincipled','Clear ice highlights')
    t.links.new(palette.outputs[0],ice.inputs['Base Color'])
    t.links.new(controls['ice_roughness'],ice.inputs['Roughness'])
    ice.inputs['IOR'].default_value=1.31;ice.inputs['Coat Weight'].default_value=.65
    ice.inputs['Coat Roughness'].default_value=.09
    frost=node(t,'ShaderNodeBsdfPrincipled','White surface frost')
    frost.inputs['Base Color'].default_value=(.78,.9,.98,1);frost.inputs['Roughness'].default_value=.62
    t.links.new(bump.outputs['Normal'],frost.inputs['Normal'])
    transparent=node(t,'ShaderNodeBsdfTransparent','See original model')
    rim=node(t,'ShaderNodeFresnel','Ice rim');rim.inputs['IOR'].default_value=1.31
    clarity=calc(t,'ADD',calc(t,'MULTIPLY',controls['ice_clarity'],calc(t,'SUBTRACT',1,i.outputs['Spike'])),calc(t,'MULTIPLY',controls['ice_spike_clarity'],i.outputs['Spike']))
    opacity=calc(t,'ADD',calc(t,'MULTIPLY',calc(t,'SUBTRACT',1,clarity),.85),calc(t,'MULTIPLY',rim.outputs[0],.75))
    opacity.node.use_clamp=True
    clear=node(t,'ShaderNodeMixShader','Clear layer')
    t.links.new(opacity,clear.inputs[0]);t.links.new(transparent.outputs[0],clear.inputs[1]);t.links.new(ice.outputs[0],clear.inputs[2])
    snowy=node(t,'ShaderNodeMixShader','Independent white frost')
    t.links.new(patch,snowy.inputs[0]);t.links.new(clear.outputs[0],snowy.inputs[1]);t.links.new(frost.outputs[0],snowy.inputs[2])
    masked=node(t,'ShaderNodeMixShader','Paint and source alpha')
    t.links.new(calc(t,'MULTIPLY',i.outputs['Mask'],i.outputs['Alpha']),masked.inputs[0])
    t.links.new(transparent.outputs[0],masked.inputs[1]);t.links.new(snowy.outputs[0],masked.inputs[2])
    out=node(t,'NodeGroupOutput','Ice Output');t.links.new(masked.outputs[0],out.inputs[0])
    c['petrify_ice_shader']=t
    normals.instrument(t)
    return t


def prune(t):
    keep=set();stack=[n for n in t.nodes if n.type=='OUTPUT_MATERIAL']
    while stack:
        n=stack.pop()
        if n in keep:continue
        keep.add(n)
        for s in n.inputs:stack.extend(l.from_node for l in s.links)
    for n in list(t.nodes):
        if n not in keep:t.nodes.remove(n)


def shell_material(source,t):
    # Copy the real alpha and mask graphs, not approximate texture filenames.
    m=source.copy();m.name=source.name+' · 冰壳';m.use_fake_user=False
    for k in list(m.keys()):del m[k]
    m['petrify_ice_owned']=True;m.use_backface_culling=True
    m.surface_render_method='DITHERED'
    n=m.node_tree.nodes;links=m.node_tree.links
    rock=n.get('Petrify Stone');strength=n.get('Petrify Strength')
    ice=node(m.node_tree,'ShaderNodeGroup','Independent Ice');ice.node_tree=t
    if strength:links.new(strength.outputs[0],ice.inputs['Mask'])
    else:ice.inputs['Mask'].default_value=0
    if rock:
        alpha=rock.inputs['Alpha']
        if alpha.is_linked:links.new(alpha.links[0].from_socket,ice.inputs['Alpha'])
        else:ice.inputs['Alpha'].default_value=alpha.default_value
    normals.bind(m,ice)
    for out in list(n):
        if out.type=='OUTPUT_MATERIAL':links.new(ice.outputs[0],out.inputs['Surface'])
    prune(m.node_tree)
    return m


def remove(ob):
    c=companion(ob)
    if not c:return
    mesh=c.data;groups=[m.node_group for m in c.modifiers if m.type=='NODES']
    groups.append(c.get('petrify_ice_shader'))
    mats=[v for v in c.get('petrify_ice_materials',[]) if isinstance(v,bpy.types.Material)]
    spike=c.get('petrify_ice_spike_material')
    if isinstance(spike,bpy.types.Material):mats.append(spike)
    del ob['petrify_ice_object'];bpy.data.objects.remove(c,do_unlink=True)
    if mesh.users==0:bpy.data.meshes.remove(mesh)
    for g in groups:
        if g and g.users==0:bpy.data.node_groups.remove(g)
    for m in mats:
        if m.users==0:bpy.data.materials.remove(m)
    # Material deletion releases the optical group last.
    for g in list(bpy.data.node_groups):
        if g.get('petrify_ice_owned') and g.users==0:bpy.data.node_groups.remove(g)


def anchors(ob,mesh,seed,api):
    """Area-weighted triangle anchors; positions follow deformation, no resampling."""
    mesh.calc_loop_triangles()
    excluded=[x.strip().casefold() for x in config(ob)['ice_exclude_materials'].replace('，',',').split(',') if x.strip()]
    allowed=set()
    for j,m in enumerate(ob.data.materials):
        if not m or any(x in m.name.casefold() for x in excluded):continue
        _,_,alpha,_,_=api.lightweight._base_description(m)
        if alpha>.01:allowed.add(j)
    tris=[tri for tri in mesh.loop_triangles if tri.material_index in allowed]
    total=0;cdf=[]
    for tri in tris:total+=tri.area;cdf.append(total)
    anchor_count=CAP if total>1e-12 else 0
    rng=random.Random(seed);indices=[[],[],[]];weights=[[],[],[]]
    for _ in range(anchor_count):
        tri=tris[min(bisect_left(cdf,rng.random()*total),len(tris)-1)]
        a=math.sqrt(rng.random());b=rng.random();w=(1-a,a*(1-b),a*b)
        for j in range(3):indices[j].append(tri.vertices[j]);weights[j].append(w[j])
    c=companion(ob);old=c.data
    host=bpy.data.meshes.new(ob.name+' · 固定冰锥锚点')
    host.from_pydata([(0,0,0)]*anchor_count,[],[])
    host.attributes.new('IceStableID','INT','POINT').data.foreach_set('value',list(range(anchor_count)))
    for j in range(3):
        host.attributes.new('IceIndex'+str(j),'INT','POINT').data.foreach_set('value',indices[j])
        host.attributes.new('IceWeight'+str(j),'FLOAT','POINT').data.foreach_set('value',weights[j])
    c.data=host
    if old.users==0:bpy.data.meshes.remove(old)
    c['source_vertex_count']=len(mesh.vertices)
    c['source_height']=max(max(v.co.z for v in mesh.vertices)-min(v.co.z for v in mesh.vertices),.001)


def attribute(t,name,kind='FLOAT'):
    n=node(t,'GeometryNodeInputNamedAttribute',name);n.data_type=kind;n.inputs['Name'].default_value=name
    return n.outputs['Attribute']


def geometry_mask(t,ob,api):
    mask=attribute(t,api.STATIC_ATTR)
    frame=node(t,'GeometryNodeInputSceneTime','Timeline').outputs['Frame']
    for g in api.stroke_groups(ob):
        if not g.get('asfr_visible', 1.0): continue
        start=g.nodes['Start'].outputs[0].default_value;end=g.nodes['End'].outputs[0].default_value
        fade=max(g.nodes['Fade'].outputs[0].default_value,.001);span=end-start
        weight=attribute(t,g['weight_attr']);mode=g.get('timing_mode','SEQUENTIAL')
        if mode=='SIMULTANEOUS':
            ramp=calc(t,'DIVIDE',calc(t,'SUBTRACT',frame,start),max(span,.001));ramp.node.use_clamp=True
            ramp=calc(t,'MAXIMUM',ramp,1 if span<.001 else 0)
        else:
            progress=attribute(t,g['segment_attr'] if mode=='PARALLEL' else g['progress_attr'])
            arrival=calc(t,'ADD',start,calc(t,'MULTIPLY',calc(t,'DIVIDE',progress,weight),span))
            ramp=calc(t,'DIVIDE',calc(t,'ADD',calc(t,'SUBTRACT',frame,arrival),fade),fade);ramp.node.use_clamp=True
        ramp=calc(t,'MULTIPLY',ramp,calc(t,'SUBTRACT',1,calc(t,'LESS_THAN',frame,start)))
        mask=calc(t,'MAXIMUM',mask,calc(t,'MULTIPLY',weight,ramp))
    strength=next((m.node_tree.nodes.get('Petrify Strength') for m in ob.data.materials if m and m.use_nodes and m.node_tree.nodes.get('Petrify Strength')),None)
    return calc(t,'MULTIPLY',mask,value(t,'Display strength',strength.inputs[1].default_value if strength else 1))


def sample(t,geometry,field,idx,kind='FLOAT'):
    n=node(t,'GeometryNodeSampleIndex','Attached sample');n.data_type=kind;n.domain='POINT';n.clamp=False
    link(t,geometry,n.inputs['Geometry']);link(t,field,n.inputs['Value']);link(t,idx,n.inputs['Index'])
    return n.outputs['Value']


def build_geometry(ob,api):
    c=companion(ob);mod=c.modifiers.get('冰层与冰锥')
    if mod is None:mod=c.modifiers.new('冰层与冰锥','NODES')
    t=mod.node_group
    if t is None:
        t=bpy.data.node_groups.new(ob.name+' · 冰层附着','GeometryNodeTree');mod.node_group=t
        socket(t,'Geometry','NodeSocketGeometry');socket(t,'Geometry','NodeSocketGeometry','OUTPUT')
        t['petrify_ice_owned']=True
    t.nodes.clear()
    source=node(t,'GeometryNodeObjectInfo','Evaluated original');source.transform_space='RELATIVE'
    source.inputs['Object'].default_value=ob;geometry=source.outputs['Geometry']
    inp=node(t,'NodeGroupInput','Fixed anchors');out=node(t,'NodeGroupOutput','Separate Ice')
    mask=geometry_mask(t,ob,api)
    normal=node(t,'GeometryNodeInputNormal','Surface normal').outputs[0]
    pos=node(t,'GeometryNodeInputPosition','Surface position').outputs[0]
    h=c['source_height'];thickness=value(t,'Thickness',h*.0016)
    offset=vec(t,'SCALE',normal,scale=thickness)
    shell=node(t,'GeometryNodeSetPosition','Thin shell');link(t,geometry,shell.inputs['Geometry']);link(t,offset,shell.inputs['Offset'])
    geom=shell.outputs[0]
    index=node(t,'GeometryNodeInputMaterialIndex','Original slot').outputs[0]
    for j,m in enumerate(c.get('petrify_ice_materials',[])):
        setmat=node(t,'GeometryNodeSetMaterial','Shell material '+str(j));setmat.inputs['Material'].default_value=m
        link(t,geom,setmat.inputs['Geometry']);link(t,calc(t,'COMPARE',index,j),setmat.inputs['Selection']);geom=setmat.outputs[0]
    coords=[];normals=[];masks=[]
    for j in range(3):
        idx=attribute(t,'IceIndex'+str(j),'INT');w=attribute(t,'IceWeight'+str(j))
        coords.append(vec(t,'SCALE',sample(t,geometry,pos,idx,'FLOAT_VECTOR'),scale=w))
        normals.append(vec(t,'SCALE',sample(t,geometry,normal,idx,'FLOAT_VECTOR'),scale=w))
        masks.append(calc(t,'MULTIPLY',sample(t,geometry,mask,idx),w))
    point=vec(t,'ADD',vec(t,'ADD',coords[0],coords[1]),coords[2])
    nrm=vec(t,'NORMALIZE',vec(t,'ADD',vec(t,'ADD',normals[0],normals[1]),normals[2]))
    weight=calc(t,'ADD',calc(t,'ADD',masks[0],masks[1]),masks[2])
    # A count guard prevents dangerous sample correspondence after topology edits.
    size=node(t,'GeometryNodeAttributeDomainSize','Source topology');size.component='MESH';link(t,geometry,size.inputs['Geometry'])
    valid=calc(t,'COMPARE',size.outputs['Point Count'],c['source_vertex_count'])
    weight=calc(t,'MULTIPLY',weight,valid)
    count=value(t,'Count',180);pointindex=node(t,'GeometryNodeInputIndex','Anchor index').outputs[0]
    select=calc(t,'MULTIPLY',calc(t,'LESS_THAN',pointindex,count),calc(t,'GREATER_THAN',weight,.001))
    points=node(t,'GeometryNodeMeshToPoints','Selected anchors');points.mode='VERTICES'
    link(t,inp.outputs[0],points.inputs['Mesh']);link(t,select,points.inputs['Selection'])
    link(t,vec(t,'ADD',point,vec(t,'SCALE',nrm,scale=thickness)),points.inputs['Position'])
    # Capture the interpolated weight/normal BEFORE topology changes to points.
    # MeshToPoints fields are evaluated on input anchors. Store attributes there
    # so subsequent fields do not accidentally resample with reindexed IDs.
    anchor_geom=inp.outputs[0]
    for name,field,kind in [('IceGrowth',weight,'FLOAT'),('IceNormal',nrm,'FLOAT_VECTOR')]:
        store=node(t,'GeometryNodeStoreNamedAttribute',name);store.data_type=kind;store.domain='POINT'
        store.inputs['Name'].default_value=name;link(t,anchor_geom,store.inputs['Geometry']);link(t,field,store.inputs['Value']);anchor_geom=store.outputs[0]
    link(t,anchor_geom,points.inputs['Mesh'])
    growth=attribute(t,'IceGrowth');direction=attribute(t,'IceNormal','FLOAT_VECTOR')
    down=value(t,'Downward',.85)
    world=node(t,'GeometryNodeObjectInfo','World orientation');world.transform_space='ORIGINAL';world.inputs['Object'].default_value=ob
    rotation=node(t,'ShaderNodeVectorRotate','World gravity');rotation.rotation_type='EULER_XYZ';rotation.invert=True
    rotation.inputs['Vector'].default_value=(0,0,-1);link(t,world.outputs['Rotation'],rotation.inputs['Rotation'])
    direction=vec(t,'ADD',vec(t,'SCALE',direction,scale=calc(t,'SUBTRACT',1,down)),vec(t,'SCALE',rotation.outputs[0],scale=down))
    align=node(t,'FunctionNodeAlignEulerToVector','Spike direction');align.axis='Z';link(t,direction,align.inputs['Vector'])
    cone=node(t,'GeometryNodeMeshCone','Ice shape');cone.inputs['Vertices'].default_value=8
    cone.inputs['Side Segments'].default_value=3;cone.inputs['Radius Top'].default_value=0
    cone.inputs['Radius Bottom'].default_value=1;cone.inputs['Depth'].default_value=1
    # Geometry Nodes Cone is already rooted at z=0 (not centered like bpy.ops).
    move=node(t,'GeometryNodeTransform','Base at surface');link(t,cone.outputs['Mesh'],move.inputs['Geometry'])
    smooth=node(t,'GeometryNodeSetShadeSmooth','Ice facets');link(t,move.outputs[0],smooth.inputs['Geometry'])
    mat=node(t,'GeometryNodeSetMaterial','Spike material');mat.inputs['Material'].default_value=c['petrify_ice_spike_material'];link(t,smooth.outputs[0],mat.inputs['Geometry'])
    rand=node(t,'FunctionNodeRandomValue','Stable size variation');rand.data_type='FLOAT';rand.inputs['Seed'].default_value=17
    rand.inputs['Min'].default_value=.4;rand.inputs['Max'].default_value=1
    link(t,attribute(t,'IceStableID','INT'),rand.inputs['ID'])
    factor=calc(t,'MULTIPLY',growth,rand.outputs['Value'])
    scale=node(t,'ShaderNodeCombineXYZ','Spike dimensions')
    width=calc(t,'MULTIPLY',value(t,'Width',h*.0065),factor)
    link(t,width,scale.inputs['X']);link(t,width,scale.inputs['Y'])
    link(t,calc(t,'MULTIPLY',value(t,'Length',h*.06),factor),scale.inputs['Z'])
    instance=node(t,'GeometryNodeInstanceOnPoints','Stable ice spikes');link(t,points.outputs[0],instance.inputs['Points'])
    link(t,mat.outputs[0],instance.inputs['Instance']);link(t,align.outputs[0],instance.inputs['Rotation']);link(t,scale.outputs[0],instance.inputs['Scale'])
    join=node(t,'GeometryNodeJoinGeometry','Shell plus ice spikes');link(t,geom,join.inputs['Geometry']);link(t,instance.outputs[0],join.inputs['Geometry'])
    link(t,join.outputs[0],out.inputs[0])


def ensure_direction_graph(t):
    """Lazily upgrade an existing 3.4.0 graph without rebuilding its anchors.

    The legacy field is retained verbatim. Random directions use the permanent
    anchor ID, never point order or frame, so changing masks cannot reshuffle.
    World vectors are converted with the full inverse matrix (including scale).
    """
    if t.nodes.get('Direction Mode') is not None:return
    align=t.nodes['Spike direction']
    legacy=align.inputs['Vector'].links[0].from_socket
    mode=value(t,'Direction Mode',0)
    space=value(t,'Direction World Space',1)
    axis=node(t,'ShaderNodeCombineXYZ','Custom Direction');axis.inputs['Z'].default_value=-1
    basis=node(t,'FunctionNodeAlignEulerToVector','Direction Basis');basis.axis='Z'
    link(t,axis.outputs[0],basis.inputs['Vector'])
    stable_id=attribute(t,'IceStableID','INT')
    azimuth=node(t,'FunctionNodeRandomValue','Direction Azimuth Random');azimuth.data_type='FLOAT'
    azimuth.inputs['Min'].default_value=0;azimuth.inputs['Max'].default_value=2*math.pi
    inclination=node(t,'FunctionNodeRandomValue','Direction Inclination Random');inclination.data_type='FLOAT'
    inclination.inputs['Min'].default_value=1;inclination.inputs['Max'].default_value=1
    link(t,stable_id,azimuth.inputs['ID']);link(t,stable_id,inclination.inputs['ID'])
    # Uniform solid angle in a cone: cos(theta) is uniform, not theta.
    z=inclination.outputs['Value'];phi=azimuth.outputs['Value']
    radius=calc(t,'SQRT',calc(t,'MAXIMUM',calc(t,'SUBTRACT',1,calc(t,'MULTIPLY',z,z)),0))
    cone=node(t,'ShaderNodeCombineXYZ','Direction Cone')
    link(t,calc(t,'MULTIPLY',radius,calc(t,'COSINE',phi)),cone.inputs['X'])
    link(t,calc(t,'MULTIPLY',radius,calc(t,'SINE',phi)),cone.inputs['Y']);link(t,z,cone.inputs['Z'])
    rotate=node(t,'ShaderNodeVectorRotate','Orient Random Cone');rotate.rotation_type='EULER_XYZ'
    link(t,cone.outputs[0],rotate.inputs['Vector']);link(t,basis.outputs[0],rotate.inputs['Rotation'])
    inverse=node(t,'FunctionNodeInvertMatrix','World To Ice Local')
    link(t,t.nodes['World orientation'].outputs['Transform'],inverse.inputs['Matrix'])
    transform=node(t,'FunctionNodeTransformDirection','World Ice Direction')
    link(t,inverse.outputs[0],transform.inputs['Transform']);link(t,rotate.outputs[0],transform.inputs['Direction'])
    select_space=node(t,'GeometryNodeSwitch','Direction Coordinate Space');select_space.input_type='VECTOR'
    link(t,space,select_space.inputs['Switch']);link(t,rotate.outputs[0],select_space.inputs['False']);link(t,transform.outputs[0],select_space.inputs['True'])
    select_mode=node(t,'GeometryNodeSwitch','Legacy Or Custom Direction');select_mode.input_type='VECTOR'
    link(t,mode,select_mode.inputs['Switch']);link(t,legacy,select_mode.inputs['False']);link(t,select_space.outputs[0],select_mode.inputs['True'])
    link(t,select_mode.outputs[0],align.inputs['Vector'])


def apply_direction_values(t,data):
    ensure_direction_graph(t)
    mode=data['ice_direction_mode']
    t.nodes['Direction Mode'].outputs[0].default_value=0 if mode=='LEGACY' else 1
    t.nodes['Direction World Space'].outputs[0].default_value=data['ice_direction_space']=='WORLD'
    az=data['ice_direction_azimuth'];el=data['ice_direction_elevation']
    axis=(math.cos(el)*math.cos(az),math.cos(el)*math.sin(az),math.sin(el))
    for s,v in zip(t.nodes['Custom Direction'].inputs,axis):s.default_value=v
    spread=data['ice_direction_spread'] if mode=='RANDOM' else 0
    t.nodes['Direction Inclination Random'].inputs['Min'].default_value=math.cos(spread)
    seed=data['ice_direction_seed']
    t.nodes['Direction Azimuth Random'].inputs['Seed'].default_value=seed
    t.nodes['Direction Inclination Random'].inputs['Seed'].default_value=seed+104729


def refresh(ob,api,reseed=False):
    if not api.stone_for(ob) or api.stone_for(ob).get('petrify_preset')!=KEY:
        remove(ob);return
    c=companion(ob)
    if c is None:
        mesh=bpy.data.meshes.new(ob.name+' · 冰锚点');c=bpy.data.objects.new(ob.name+' · 冰层与冰锥',mesh)
        ob.users_collection[0].objects.link(c);c[TAG]=True;c[OWNER]=ob
        c.parent=ob;c.hide_select=True;ob['petrify_ice_object']=c
        reseed=True
    if reseed:
        dg=bpy.context.evaluated_depsgraph_get();ev=ob.evaluated_get(dg)
        mesh=ev.to_mesh(preserve_all_data_layers=True,depsgraph=dg)
        try:anchors(ob,mesh,int(config(ob)['ice_seed']),api)
        finally:ev.to_mesh_clear()
    optical=shader(ob)
    old=list(c.get('petrify_ice_materials',[]))
    mats=[shell_material(m,optical) for m in ob.data.materials if m and m.use_nodes]
    if len(mats)!=len(ob.data.materials):raise RuntimeError('请先初始化所有材质槽后再添加冰层')
    c['petrify_ice_materials']=mats
    if not c.get('petrify_ice_spike_material'):
        m=bpy.data.materials.new(ob.name+' · 冰锥');m.use_nodes=True;m['petrify_ice_owned']=True
        m.surface_render_method='DITHERED';m.node_tree.nodes.clear()
        ice=node(m.node_tree,'ShaderNodeGroup','Ice');ice.node_tree=optical;ice.inputs['Spike'].default_value=1
        out=node(m.node_tree,'ShaderNodeOutputMaterial','Output');m.node_tree.links.new(ice.outputs[0],out.inputs['Surface'])
        c['petrify_ice_spike_material']=m
    build_geometry(ob,api)
    for m in old:
        if m.users==0:bpy.data.materials.remove(m)
    c['petrify_normal_shell']=normals.VERSION
    apply_values(ob)


def apply_values(ob):
    c=companion(ob)
    if not c:return
    data=config(ob);t=shader(ob);h=c['source_height']
    for key in ('ice_clarity','ice_frost','ice_tint','ice_spike_clarity'):t.nodes[key].outputs[0].default_value=data[key]/100
    t.nodes['ice_roughness'].outputs[0].default_value=data['ice_roughness']
    t.nodes['ice_color'].outputs[0].default_value=data['ice_color']
    t.nodes['bump_distance'].outputs[0].default_value=h*.00012
    g=c.modifiers['冰层与冰锥'].node_group
    apply_direction_values(g,data)
    for name,v in [('Thickness',max(h*data['ice_thickness']/100,h*.00003)),('Count',data['ice_count']),('Length',h*data['ice_length']/100),('Width',h*data['ice_width']/100),('Downward',data['ice_down'])]:
        g.nodes[name].outputs[0].default_value=v
    g.nodes['Stable size variation'].inputs['Min'].default_value=1-data['ice_random']*.95
    g.nodes['Ice shape'].inputs['Vertices'].default_value=8 if data['ice_style']=='ICICLE' else 5
    g.nodes['Ice facets'].inputs['Shade Smooth'].default_value=data['ice_style']=='ICICLE'
    strength=next((m.node_tree.nodes.get('Petrify Strength') for m in ob.data.materials if m and m.use_nodes and m.node_tree.nodes.get('Petrify Strength')),None)
    if strength:
        v=strength.inputs[1].default_value;g.nodes['Display strength'].outputs[0].default_value=v
        for m in c['petrify_ice_materials']:
            n=m.node_tree.nodes.get('Petrify Strength')
            if n:n.inputs[1].default_value=v
    normals.apply_values(ob)
    c.hide_render=ob.hide_render


def update(settings,ob):
    ob['petrify_ice_settings']={k:getattr(settings,k) for k in DEFAULTS}
    apply_values(ob)


def refresh_all(api):
    for ob in list(bpy.data.objects):
        if companion(ob):refresh(ob,api)


def draw(box,s):
    box.label(text='独立冰壳：人物原材质保持不变')
    for name in ('ice_clarity','ice_frost','ice_tint'):box.prop(s,name,slider=True)
    box.prop(s,'ice_color');box.prop(s,'ice_roughness');box.prop(s,'ice_thickness')
    box.separator();box.label(text='附着冰锥（数量上限 1000 / 网格）')
    for name in ('ice_style','ice_count','ice_length','ice_width','ice_random'):box.prop(s,name)
    box.prop(s,'ice_direction_mode')
    if s.ice_direction_mode=='LEGACY':
        box.prop(s,'ice_down')
        box.label(text='保留原有沿表面 / 垂挂的自然朝向')
    else:
        box.prop(s,'ice_direction_space')
        box.prop(s,'ice_direction_azimuth');box.prop(s,'ice_direction_elevation')
        row=box.row(align=True)
        for axis,label in [('PX','+X'),('NX','−X'),('PY','+Y'),('NY','−Y'),('PZ','向上'),('NZ','向下')]:
            row.operator('petrify.ice_direction_axis',text=label).axis=axis
        if s.ice_direction_mode=='RANDOM':
            box.prop(s,'ice_direction_spread');box.prop(s,'ice_direction_seed')
            box.label(text='偏转 0° 统一；180° 全方向随机')
        box.label(text='水平角 0° = +X；90° = +Y')
    for name in ('ice_spike_clarity','ice_seed','ice_exclude_materials'):box.prop(s,name)
    box.operator('petrify.reseed_ice',icon='FILE_REFRESH')
    box.label(text='长/粗/厚以模型高度百分比计')
    if s.ice_direction_mode=='LEGACY':box.label(text='向下比例 0 沿表面向外；1 世界向下')
    box.label(text='冰锥仅跟随新版球形/表面路径')
    box.label(text='轻量预览仅看范围；关闭后看冰效果')
