%% Project 1: Laplace Equation Around a Cylinder
% Solves the 2D Laplace equation in polar coordinates using
% second-order finite differences and a direct solver (LU).
%
% PDE: (1/r)*d/dr(r*du/dr) + (1/r^2)*d2u/dtheta2 = 0
% Domain: theta in [0, 2*pi], r in [1, 2]
% Analytical solution: u(r,theta) = (r - 1/r)*sin(theta)
% BCs: u(1,theta) = 0,  u(2,theta) = (2 - 1/2)*sin(theta)

clear all;
close all;
clc;

%% ========== PART 1: Solve on three meshes ==========

Ntheta_list = [81, 161, 321];
Nr_list     = [41,  81, 161];
num_meshes  = 3;

errors     = zeros(num_meshes, 1);
dr_vals    = zeros(num_meshes, 1);
dtheta_vals = zeros(num_meshes, 1);

for mesh_idx = 1:num_meshes

    Ntheta = Ntheta_list(mesh_idx);
    Nr     = Nr_list(mesh_idx);

    dtheta = 2*pi / (Ntheta - 1);
    dr     = 1 / (Nr - 1);

    dr_vals(mesh_idx)    = dr;
    dtheta_vals(mesh_idx) = dtheta;

    % Build grid arrays
    theta = zeros(Ntheta, 1);
    for i = 1:Ntheta
        theta(i) = (i - 1) * dtheta;
    end

    r = zeros(Nr, 1);
    for j = 1:Nr
        r(j) = 1 + (j - 1) * dr;
    end

    Nt = Ntheta - 1;       % unique theta points (periodic)
    Nri = Nr - 2;           % interior r points (j=2 to Nr-1)
    N_unknowns = Nt * Nri;

    % Preallocate sparse matrix triplets
    nnz_est = 5 * N_unknowns;
    I_sp = zeros(nnz_est, 1);
    J_sp = zeros(nnz_est, 1);
    V_sp = zeros(nnz_est, 1);
    rhs  = zeros(N_unknowns, 1);
    cnt  = 0;

    for i = 1:Nt
        for k = 1:Nri
            j  = k + 1;
            rj = r(j);
            row = (i - 1)*Nri + k;

            % Second-order FD coefficients from the expanded Laplacian
            c_center = -2/dr^2 - 2/(rj^2 * dtheta^2);
            c_rm     =  1/dr^2 - 1/(2*rj*dr);
            c_rp     =  1/dr^2 + 1/(2*rj*dr);
            c_th     =  1/(rj^2 * dtheta^2);

            % center
            cnt = cnt + 1;
            I_sp(cnt) = row;  J_sp(cnt) = row;  V_sp(cnt) = c_center;

            % r-minus neighbor
            if j - 1 == 1
                % inner BC: u(r=1) = 0, nothing added
            else
                col = (i - 1)*Nri + (k - 1);
                cnt = cnt + 1;
                I_sp(cnt) = row;  J_sp(cnt) = col;  V_sp(cnt) = c_rm;
            end

            % r-plus neighbor
            if j + 1 == Nr
                u_bc = (2 - 0.5) * sin(theta(i));
                rhs(row) = rhs(row) - c_rp * u_bc;
            else
                col = (i - 1)*Nri + (k + 1);
                cnt = cnt + 1;
                I_sp(cnt) = row;  J_sp(cnt) = col;  V_sp(cnt) = c_rp;
            end

            % theta-minus (periodic)
            if i == 1
                i_m = Nt;
            else
                i_m = i - 1;
            end
            col = (i_m - 1)*Nri + k;
            cnt = cnt + 1;
            I_sp(cnt) = row;  J_sp(cnt) = col;  V_sp(cnt) = c_th;

            % theta-plus (periodic)
            if i == Nt
                i_p = 1;
            else
                i_p = i + 1;
            end
            col = (i_p - 1)*Nri + k;
            cnt = cnt + 1;
            I_sp(cnt) = row;  J_sp(cnt) = col;  V_sp(cnt) = c_th;
        end
    end

    I_sp = I_sp(1:cnt);
    J_sp = J_sp(1:cnt);
    V_sp = V_sp(1:cnt);

    A = sparse(I_sp, J_sp, V_sp, N_unknowns, N_unknowns);

    % Solve using direct solver (LU factorization)
    u_vec = A \ rhs;

    % Reconstruct full solution on the grid
    u_num   = zeros(Ntheta, Nr);
    u_exact = zeros(Ntheta, Nr);

    for i = 1:Ntheta
        u_num(i, 1)  = 0;
        u_num(i, Nr) = (2 - 0.5) * sin(theta(i));
    end

    for i = 1:Nt
        for k = 1:Nri
            idx = (i - 1)*Nri + k;
            u_num(i, k + 1) = u_vec(idx);
        end
    end
    u_num(Ntheta, :) = u_num(1, :);

    for i = 1:Ntheta
        for j = 1:Nr
            u_exact(i, j) = (r(j) - 1/r(j)) * sin(theta(i));
        end
    end

    % Error norm
    err_sum = 0;
    for i = 1:Ntheta
        for j = 1:Nr
            err_sum = err_sum + (u_num(i,j) - u_exact(i,j))^2;
        end
    end
    errors(mesh_idx) = sqrt(err_sum) / sqrt(Ntheta * Nr);

    fprintf('Mesh %d (%3d x %3d): dr = %.6f, dtheta = %.6f, Error = %.6e\n', ...
        mesh_idx, Ntheta, Nr, dr, dtheta, errors(mesh_idx));

    if mesh_idx == num_meshes
        u_finest     = u_num;
        u_ex_finest  = u_exact;
        theta_finest = theta;
        r_finest     = r;
    end
end

%% ========== PART 2: Convergence Rates ==========

fprintf('\n--- Spatial Convergence Rates ---\n');
for m = 2:num_meshes
    rate_r     = log(errors(m-1)/errors(m)) / log(dr_vals(m-1)/dr_vals(m));
    rate_theta = log(errors(m-1)/errors(m)) / log(dtheta_vals(m-1)/dtheta_vals(m));
    fprintf('Mesh %d -> %d:  rate (dr) = %.4f,  rate (dtheta) = %.4f\n', ...
        m-1, m, rate_r, rate_theta);
end

%% ========== PART 2: Log-Log Error Plots ==========

% Error vs dr
fig1 = figure(1);
loglog(dr_vals, errors, 'bo-', 'LineWidth', 2, 'MarkerSize', 10);
hold on;
ref2 = errors(1) * (dr_vals / dr_vals(1)).^2;
loglog(dr_vals, ref2, 'r--', 'LineWidth', 1.5);
xlabel('\Deltar', 'FontSize', 14);
ylabel('Error', 'FontSize', 14);
title('Error vs \Deltar (log-log scale)', 'FontSize', 14);
legend('Numerical Error', '2nd Order Reference', 'Location', 'NorthWest');
grid on;
hold off;
print(fig1, 'error_vs_dr', '-dpng', '-r300');

% Error vs dtheta
fig2 = figure(2);
loglog(dtheta_vals, errors, 'rs-', 'LineWidth', 2, 'MarkerSize', 10);
hold on;
ref2t = errors(1) * (dtheta_vals / dtheta_vals(1)).^2;
loglog(dtheta_vals, ref2t, 'b--', 'LineWidth', 1.5);
xlabel('\Delta\theta', 'FontSize', 14);
ylabel('Error', 'FontSize', 14);
title('Error vs \Delta\theta (log-log scale)', 'FontSize', 14);
legend('Numerical Error', '2nd Order Reference', 'Location', 'NorthWest');
grid on;
hold off;
print(fig2, 'error_vs_dtheta', '-dpng', '-r300');

%% ========== Contour Plots on Finest Mesh ==========

Ntheta_f = length(theta_finest);
Nr_f     = length(r_finest);

X = zeros(Ntheta_f, Nr_f);
Y = zeros(Ntheta_f, Nr_f);
for i = 1:Ntheta_f
    for j = 1:Nr_f
        X(i,j) = r_finest(j) * cos(theta_finest(i));
        Y(i,j) = r_finest(j) * sin(theta_finest(i));
    end
end

% Numerical solution contour
fig3 = figure(3);
contourf(X, Y, u_finest, 30);
colorbar;
xlabel('x', 'FontSize', 14);
ylabel('y', 'FontSize', 14);
title('Numerical Solution u(r,\theta) - 321\times161 Mesh', 'FontSize', 14);
axis equal;
print(fig3, 'numerical_solution', '-dpng', '-r300');

% Analytical solution contour
fig4 = figure(4);
contourf(X, Y, u_ex_finest, 30);
colorbar;
xlabel('x', 'FontSize', 14);
ylabel('y', 'FontSize', 14);
title('Analytical Solution u(r,\theta)', 'FontSize', 14);
axis equal;
print(fig4, 'analytical_solution', '-dpng', '-r300');

% Error distribution
fig5 = figure(5);
contourf(X, Y, abs(u_finest - u_ex_finest), 20);
colorbar;
xlabel('x', 'FontSize', 14);
ylabel('y', 'FontSize', 14);
title('|u_{num} - u_{exact}| on 321\times161 Mesh', 'FontSize', 14);
axis equal;
print(fig5, 'error_distribution', '-dpng', '-r300');

%% ========== BONUS: Interior of the Cylinder (0 <= r <= 1) ==========

fprintf('\n========== BONUS: Interior of Cylinder ==========\n');

Ntheta_in = 81;
Nr_in     = 41;

dtheta_in = 2*pi / (Ntheta_in - 1);
dr_in     = 1 / (Nr_in - 1);

theta_in = zeros(Ntheta_in, 1);
for i = 1:Ntheta_in
    theta_in(i) = (i - 1) * dtheta_in;
end

r_in = zeros(Nr_in, 1);
for j = 1:Nr_in
    r_in(j) = (j - 1) * dr_in;
end

Nt_in  = Ntheta_in - 1;
Nri_in = Nr_in - 2;
N_in = 1 + Nt_in * Nri_in;

nnz_in = 5 * N_in + Nt_in;
I_in = zeros(nnz_in, 1);
J_in = zeros(nnz_in, 1);
V_in = zeros(nnz_in, 1);
rhs_in = zeros(N_in, 1);
cnt_in = 0;

% Pole equation: u(r=0) = average of neighbors on first ring
cnt_in = cnt_in + 1;
I_in(cnt_in) = 1;  J_in(cnt_in) = 1;  V_in(cnt_in) = 1;

for i = 1:Nt_in
    col = 1 + (i - 1)*Nri_in + 1;
    cnt_in = cnt_in + 1;
    I_in(cnt_in) = 1;  J_in(cnt_in) = col;  V_in(cnt_in) = -1/Nt_in;
end

% Interior equations
for i = 1:Nt_in
    for k = 1:Nri_in
        j  = k + 1;
        rj = r_in(j);
        row = 1 + (i - 1)*Nri_in + k;

        c_center = -2/dr_in^2 - 2/(rj^2 * dtheta_in^2);
        c_rm     =  1/dr_in^2 - 1/(2*rj*dr_in);
        c_rp     =  1/dr_in^2 + 1/(2*rj*dr_in);
        c_th     =  1/(rj^2 * dtheta_in^2);

        cnt_in = cnt_in + 1;
        I_in(cnt_in) = row;  J_in(cnt_in) = row;  V_in(cnt_in) = c_center;

        if j - 1 == 1
            cnt_in = cnt_in + 1;
            I_in(cnt_in) = row;  J_in(cnt_in) = 1;  V_in(cnt_in) = c_rm;
        else
            col = 1 + (i - 1)*Nri_in + (k - 1);
            cnt_in = cnt_in + 1;
            I_in(cnt_in) = row;  J_in(cnt_in) = col;  V_in(cnt_in) = c_rm;
        end

        if j + 1 == Nr_in
            % u(r=1) = 0 -> nothing
        else
            col = 1 + (i - 1)*Nri_in + (k + 1);
            cnt_in = cnt_in + 1;
            I_in(cnt_in) = row;  J_in(cnt_in) = col;  V_in(cnt_in) = c_rp;
        end

        if i == 1
            i_m = Nt_in;
        else
            i_m = i - 1;
        end
        col = 1 + (i_m - 1)*Nri_in + k;
        cnt_in = cnt_in + 1;
        I_in(cnt_in) = row;  J_in(cnt_in) = col;  V_in(cnt_in) = c_th;

        if i == Nt_in
            i_p = 1;
        else
            i_p = i + 1;
        end
        col = 1 + (i_p - 1)*Nri_in + k;
        cnt_in = cnt_in + 1;
        I_in(cnt_in) = row;  J_in(cnt_in) = col;  V_in(cnt_in) = c_th;
    end
end

I_in = I_in(1:cnt_in);
J_in = J_in(1:cnt_in);
V_in = V_in(1:cnt_in);

A_in = sparse(I_in, J_in, V_in, N_in, N_in);
u_in_vec = A_in \ rhs_in;

% Reconstruct interior solution
u_interior = zeros(Ntheta_in, Nr_in);
for i = 1:Ntheta_in
    u_interior(i, 1) = u_in_vec(1);
end
for i = 1:Nt_in
    for k = 1:Nri_in
        idx = 1 + (i - 1)*Nri_in + k;
        u_interior(i, k + 1) = u_in_vec(idx);
    end
end
u_interior(Ntheta_in, :) = u_interior(1, :);

fprintf('Max |u| inside cylinder = %.6e\n', max(abs(u_interior(:))));

% Interior contour plot
X_in = zeros(Ntheta_in, Nr_in);
Y_in = zeros(Ntheta_in, Nr_in);
for i = 1:Ntheta_in
    for j = 1:Nr_in
        X_in(i,j) = r_in(j) * cos(theta_in(i));
        Y_in(i,j) = r_in(j) * sin(theta_in(i));
    end
end

fig6 = figure(6);
pcolor(X_in, Y_in, u_interior);
shading interp;
colorbar;
xlabel('x', 'FontSize', 14);
ylabel('y', 'FontSize', 14);
title('Interior Solution (0 \leq r \leq 1)', 'FontSize', 14);
axis equal;
print(fig6, 'interior_solution', '-dpng', '-r300');

fprintf('\nDone.\n');
