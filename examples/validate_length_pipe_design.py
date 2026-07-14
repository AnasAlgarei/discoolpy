from discoolpy import load_yaml_config
from discoolpy.utils import build_standard_branch_system

cfg = load_yaml_config('../configs/config_length_derived_pr.yaml')
nw, chill, buildings, branch, cooling_tower, c = build_standard_branch_system(cfg)
print('Solving length-based pipe design case...')
nw.solve(mode='design', max_iter=int(cfg.get('solver', {}).get('max_iter', 250)))
print('converged:', getattr(nw, 'converged', None))
print('iterations:', getattr(nw, 'iter', None))
print('residual:', getattr(nw, 'residual', None))
print('branch_out p:', branch.connections['branch_out'].p.val)
for key, conn in branch.connections.items():
    if key in ('branch_in', 'branch_out', 'building_1_in', 'building_1_out', 'building_2_in', 'building_2_out', 'building_3_in', 'building_3_out'):
        print(key, 'm=', conn.m.val_SI, 'p=', conn.p.val, 'T=', conn.T.val)
