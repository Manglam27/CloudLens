import 'package:amplify_auth_cognito/amplify_auth_cognito.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:flutter/material.dart';

/// Resets a forgotten password with an e-mailed code (FR 1.2).
///
/// Step 1 asks Cognito to e-mail a code with [Amplify.Auth.resetPassword].
/// Step 2 sets the new password with that code via [Amplify.Auth.confirmResetPassword].
/// On success the page closes and returns the e-mail, so the login page can fill it in.
class ResetPasswordPage extends StatefulWidget {
  final String initialEmail;

  const ResetPasswordPage({super.key, this.initialEmail = ''});

  @override
  State<ResetPasswordPage> createState() => _ResetPasswordPageState();
}

class _ResetPasswordPageState extends State<ResetPasswordPage> {
  late final TextEditingController emailController = TextEditingController(text: widget.initialEmail);
  final TextEditingController codeController = TextEditingController();
  final TextEditingController passwordController = TextEditingController();
  final TextEditingController confirmController = TextEditingController();

  bool _codeSent = false;
  bool _isBusy = false;
  String? _destination;

  @override
  void dispose() {
    emailController.dispose();
    codeController.dispose();
    passwordController.dispose();
    confirmController.dispose();
    super.dispose();
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _sendCode() async {
    final email = emailController.text.trim();
    if (email.isEmpty) {
      _showMessage('Enter the e-mail address you signed up with.');
      return;
    }
    setState(() => _isBusy = true);
    try {
      final result = await Amplify.Auth.resetPassword(username: email);
      if (result.nextStep.updateStep == AuthResetPasswordStep.confirmResetPasswordWithCode) {
        setState(() {
          _codeSent = true;
          _destination = result.nextStep.codeDeliveryDetails?.destination;
        });
        _showMessage('We sent a code to ${_destination ?? email}. Check your spam folder too.');
      }
    } on LimitExceededException {
      _showMessage('Too many attempts. Please wait a few minutes and try again.');
    } on AuthException catch (e) {
      _showMessage('Could not send the code: ${e.message}');
    } finally {
      if (mounted) setState(() => _isBusy = false);
    }
  }

  Future<void> _resetPassword() async {
    final email = emailController.text.trim();
    final code = codeController.text.trim();
    final password = passwordController.text;
    if (code.isEmpty || password.isEmpty) {
      _showMessage('Enter the code and a new password.');
      return;
    }
    if (password != confirmController.text) {
      _showMessage('The two passwords do not match.');
      return;
    }
    setState(() => _isBusy = true);
    try {
      final result = await Amplify.Auth.confirmResetPassword(
        username: email,
        newPassword: password,
        confirmationCode: code,
      );
      if (result.isPasswordReset && mounted) {
        Navigator.pop(context, email);
      }
    } on CodeMismatchException {
      _showMessage('That code is not correct. Please check it and try again.');
    } on ExpiredCodeException {
      _showMessage('That code has expired. Tap "Send a new code".');
    } on InvalidPasswordException catch (e) {
      _showMessage('Password does not meet the requirements: ${e.message}');
    } on LimitExceededException {
      _showMessage('Too many attempts. Please wait a few minutes and try again.');
    } on AuthException catch (e) {
      _showMessage('Could not reset the password: ${e.message}');
    } finally {
      if (mounted) setState(() => _isBusy = false);
    }
  }

  Widget _field(TextEditingController controller, String hint, IconData icon,
      {bool obscure = false, TextInputType? keyboard, bool enabled = true}) {
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.symmetric(horizontal: 16.0),
      decoration: BoxDecoration(
        color: enabled ? Colors.white : Colors.white70,
        borderRadius: BorderRadius.circular(30.0),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.1),
            spreadRadius: 2,
            blurRadius: 5,
            offset: const Offset(0, 2),
          ),
        ],
      ),
      child: TextField(
        controller: controller,
        obscureText: obscure,
        keyboardType: keyboard,
        enabled: enabled,
        decoration: InputDecoration(icon: Icon(icon), hintText: hint, border: InputBorder.none),
      ),
    );
  }

  Widget _button(String label, VoidCallback onPressed) {
    return ElevatedButton(
      onPressed: _isBusy ? null : onPressed,
      style: ElevatedButton.styleFrom(
        backgroundColor: Colors.deepPurple,
        foregroundColor: Colors.white,
        elevation: 5.0,
        minimumSize: const Size(double.infinity, 45),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(30)),
      ),
      child: _isBusy
          ? const SizedBox(
              height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
          : Text(label, style: const TextStyle(fontSize: 16.0, fontWeight: FontWeight.bold)),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      resizeToAvoidBottomInset: true,
      appBar: AppBar(title: const Text('Reset password'), backgroundColor: Colors.white, elevation: 0),
      body: Container(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Color(0xFF8EC5FC), Color(0xFFE0C3FC)],
          ),
        ),
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 20.0, vertical: 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  _codeSent
                      ? 'Enter the code we sent to ${_destination ?? emailController.text.trim()} and choose a new password.'
                      : 'Enter your e-mail address and we will send you a code to reset your password.',
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500, color: Colors.black87),
                ),
                const SizedBox(height: 24),
                _field(emailController, 'Email', Icons.email,
                    keyboard: TextInputType.emailAddress, enabled: !_codeSent),
                if (!_codeSent) _button('Send code', _sendCode),
                if (_codeSent) ...[
                  _field(codeController, 'Code from e-mail', Icons.mark_email_read, keyboard: TextInputType.number),
                  _field(passwordController, 'New password', Icons.lock, obscure: true),
                  _field(confirmController, 'Confirm new password', Icons.lock_outline, obscure: true),
                  _button('Reset password', _resetPassword),
                  const SizedBox(height: 8),
                  TextButton(
                    onPressed: _isBusy ? null : _sendCode,
                    child: const Text('Send a new code',
                        style: TextStyle(color: Colors.deepPurple, fontWeight: FontWeight.bold)),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
