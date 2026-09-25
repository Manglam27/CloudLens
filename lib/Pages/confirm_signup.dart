import 'package:amplify_auth_cognito/amplify_auth_cognito.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:cloud_lens/Pages/login.dart';
import 'package:cloud_lens/Pages/main_page.dart';
import 'package:flutter/material.dart';

/// Collects the verification code Cognito emails after sign up.
///
/// A Cognito user pool that requires email verification creates the account in
/// an UNCONFIRMED state. The account cannot sign in until the emailed code is
/// submitted through [Amplify.Auth.confirmSignUp], which is what this page does.
class ConfirmSignupPage extends StatefulWidget {
  final Future<void> Function(BuildContext) signOutCallback;

  /// The username the account was registered with.
  final String email;

  /// The password used at sign up, when it is known. Supplying it lets this
  /// page sign the user straight in once the account is confirmed, instead of
  /// sending them back to the login form.
  final String? password;

  const ConfirmSignupPage({
    super.key,
    required this.signOutCallback,
    required this.email,
    this.password,
  });

  @override
  State<ConfirmSignupPage> createState() => _ConfirmSignupPageState();
}

class _ConfirmSignupPageState extends State<ConfirmSignupPage> {
  final TextEditingController codeController = TextEditingController();

  bool _isConfirming = false;
  bool _isResending = false;

  @override
  void dispose() {
    codeController.dispose();
    super.dispose();
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _confirm() async {
    final code = codeController.text.trim();
    if (code.isEmpty) {
      _showMessage('Enter the verification code sent to your email.');
      return;
    }

    setState(() => _isConfirming = true);
    try {
      final result = await Amplify.Auth.confirmSignUp(
        username: widget.email,
        confirmationCode: code,
      );

      if (!result.isSignUpComplete) {
        _showMessage('Could not confirm the account. Please try again.');
        return;
      }

      // The account is confirmed but not yet signed in. Sign in directly when
      // the password is known, otherwise hand off to the login page.
      final password = widget.password;
      if (password != null && password.isNotEmpty) {
        final signInResult = await Amplify.Auth.signIn(
          username: widget.email,
          password: password,
        );
        if (!mounted) return;
        if (signInResult.isSignedIn) {
          Navigator.pushReplacement(
            context,
            MaterialPageRoute(
              builder: (context) =>
                  MainPage(signOutCallback: widget.signOutCallback),
            ),
          );
          return;
        }
      }

      if (!mounted) return;
      _showMessage('Account confirmed. Please sign in.');
      Navigator.pushReplacement(
        context,
        MaterialPageRoute(
          builder: (context) =>
              LoginPage(signOutCallback: widget.signOutCallback),
        ),
      );
    } on CodeMismatchException {
      _showMessage('That code is not correct. Please check and try again.');
    } on ExpiredCodeException {
      _showMessage('That code has expired. Use Resend code for a new one.');
    } on LimitExceededException {
      _showMessage('Too many attempts. Please wait a moment and try again.');
    } on AuthException catch (e) {
      _showMessage('Could not confirm the account: ${e.message}');
    } finally {
      if (mounted) setState(() => _isConfirming = false);
    }
  }

  Future<void> _resendCode() async {
    setState(() => _isResending = true);
    try {
      await Amplify.Auth.resendSignUpCode(username: widget.email);
      _showMessage('A new code has been sent to ${widget.email}.');
    } on LimitExceededException {
      _showMessage('Too many requests. Please wait a moment and try again.');
    } on AuthException catch (e) {
      _showMessage('Could not resend the code: ${e.message}');
    } finally {
      if (mounted) setState(() => _isResending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      resizeToAvoidBottomInset: false,
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        title: const Text('Verify your email'),
        backgroundColor: Colors.white,
        elevation: 0,
      ),
      body: Stack(
        children: [
          Container(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [Color(0xFF8EC5FC), Color(0xFFE0C3FC)],
              ),
            ),
          ),
          Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 20.0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.center,
                children: [
                  Container(
                    margin: const EdgeInsets.symmetric(vertical: 30.0),
                    child: Image.asset(
                      'assets/icon.png',
                      height: 150.0,
                      width: 150.0,
                    ),
                  ),
                  Text(
                    'We sent a verification code to\n${widget.email}',
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 16.0,
                      fontWeight: FontWeight.w500,
                      color: Colors.black87,
                    ),
                  ),
                  const SizedBox(height: 25.0),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 16.0),
                    decoration: BoxDecoration(
                      color: Colors.white,
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
                      controller: codeController,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(
                        icon: Icon(Icons.mark_email_read),
                        hintText: "Verification code",
                        border: InputBorder.none,
                      ),
                    ),
                  ),
                  const SizedBox(height: 30),
                  ElevatedButton(
                    onPressed: _isConfirming ? null : _confirm,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.deepPurple,
                      foregroundColor: Colors.white,
                      elevation: 5.0,
                      minimumSize: const Size(double.infinity, 45),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(30),
                      ),
                    ),
                    child: _isConfirming
                        ? const SizedBox(
                            height: 20,
                            width: 20,
                            child: CircularProgressIndicator(
                              strokeWidth: 2,
                              color: Colors.white,
                            ),
                          )
                        : const Text(
                            'Confirm',
                            style: TextStyle(
                              fontSize: 16.0,
                              fontWeight: FontWeight.bold,
                            ),
                          ),
                  ),
                  const SizedBox(height: 15.0),
                  TextButton(
                    onPressed: _isResending ? null : _resendCode,
                    child: Text(
                      _isResending ? 'Sending...' : 'Resend code',
                      style: const TextStyle(
                        color: Colors.deepPurple,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
